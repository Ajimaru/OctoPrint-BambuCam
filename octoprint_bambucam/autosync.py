"""Automatic timelapse pull after a print finishes (opt-in).

When ``auto_sync`` is on, a finished print schedules a background pull of the
printer's new timelapse(s). The pull only starts once the system is idle — the
printer is not printing AND OctoPrint's own timelapse renderer is done — so we
never run two ffmpeg encodes at once or read a half-written file. See plan §10.

``AutoSyncMixin`` is mixed into ``BambucamPlugin`` and reuses the transfer
machinery from ``TimelapseOpsMixin`` (``_run_batch``, ``_already_copied``,
``_print_active``, ``_make_ftp``) plus the plugin's settings/logger/messages.
"""

import datetime
import logging
import os
import threading
import time
from typing import TYPE_CHECKING, Optional

from octoprint.events import Events

from .ftp import FtpError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from octoprint.plugin import PluginSettings
    from octoprint.plugin.core import PluginManager

_GATE_POLL_SECONDS = 5
_GATE_MAX_WAIT_SECONDS = 1800  # 30 min

# How long to keep polling the SD card after PrintDone for the new video to
# appear, so we can stamp it with the real print-end time. The A1 mini renders
# its timelapse with a delay (measured ~+370 s); give it generous headroom.
_PRINT_DATE_MAX_WAIT_SECONDS = 1800  # 30 min
_PRINT_DATE_POLL_SECONDS = 15


class AutoSyncMixin:
    """Trigger an automatic timelapse pull when a print finishes."""

    _settings: "PluginSettings"
    _plugin_manager: "PluginManager"
    _logger: logging.Logger
    _identifier: str

    def _print_active(self) -> bool:  # pragma: no cover - overridden
        raise NotImplementedError

    def _already_copied(self, name) -> bool:  # pragma: no cover
        raise NotImplementedError

    def _run_batch(self, op, names) -> None:  # pragma: no cover
        raise NotImplementedError

    def _make_ftp(self):  # pragma: no cover
        raise NotImplementedError

    def _init_autosync(self) -> None:
        """Initialize auto-sync state. Call from the plugin ``__init__``."""
        self._autosync_lock = threading.Lock()
        self._octoprint_rendering = False
        self._autosync_cancel: Optional[threading.Event] = None
        self._own_movies: set = set()
        # SD-card videos (name -> size) snapshotted at PrintStarted, used to
        # detect this print's new/grown video at PrintDone.
        self._print_baseline: Optional[dict] = None

    def on_event(self, event, payload) -> None:
        """Track OctoPrint's render state and trigger auto-sync on print end."""
        if event == Events.MOVIE_RENDERING:
            if not self._is_own_movie(payload):
                self._octoprint_rendering = True
        elif event in (Events.MOVIE_DONE, Events.MOVIE_FAILED):
            if self._is_own_movie(payload):
                self._forget_own_movie(payload)
            else:
                self._octoprint_rendering = False
        elif event == Events.PRINT_STARTED:
            self._cancel_pending_autosync()
            threading.Thread(
                target=self._snapshot_print_baseline, daemon=True
            ).start()
        elif event == Events.PRINT_DONE:
            self._record_print_date()
            self._on_print_done()
            self._maybe_measure_render_delay()

    def note_own_movie(self, movie_path: str) -> None:
        """Record a movie path we are about to fire ``MovieDone`` for, so the
        event handler ignores it (otherwise our own copy would look like an
        OctoPrint render and flip the gate flag)."""
        if movie_path:
            self._own_movies.add(os.path.normpath(movie_path))

    def _is_own_movie(self, payload) -> bool:
        movie = (
            (payload or {}).get("movie") if isinstance(payload, dict) else None
        )
        return bool(movie and os.path.normpath(movie) in self._own_movies)

    def _forget_own_movie(self, payload) -> None:
        movie = (
            (payload or {}).get("movie") if isinstance(payload, dict) else None
        )
        if movie:
            self._own_movies.discard(os.path.normpath(movie))

    def _on_print_done(self) -> None:
        if not self._settings.get_boolean(["auto_sync"]):
            return
        delay = max(0, self._settings.get_int(["auto_sync_delay"]) or 0)
        cancel = threading.Event()
        with self._autosync_lock:
            if self._autosync_cancel is not None:
                self._autosync_cancel.set()
            self._autosync_cancel = cancel
        threading.Thread(
            target=self._autosync_worker, args=(delay, cancel), daemon=True
        ).start()

    def _cancel_pending_autosync(self) -> None:
        with self._autosync_lock:
            if self._autosync_cancel is not None:
                self._autosync_cancel.set()
                self._autosync_cancel = None

    # ------------------------------------------------------------------
    # Real print dates. Everything the A1 mini writes to the SD card carries
    # a wrong camera-subsystem clock in LAN-only mode (name, MDTM, thumbnail,
    # logs), and nothing on the card links a video to its real time. The one
    # trustworthy source is OctoPrint's own PrintDone event: we capture its
    # wall-clock time and, once the printer has rendered the .avi, map the new
    # video name to that time. Persisted in settings so uncopied videos can
    # show a real date in the tab even across restarts and with auto-sync off.
    # ------------------------------------------------------------------
    def _snapshot_print_baseline(self) -> None:
        """Record the SD-card videos present when a print starts.

        The A1 mini often finishes rendering the timelapse *during* the print
        (verified live), so a video can already be on the card by the time
        ``PrintDone`` fires. We therefore baseline at ``PrintStarted`` and look
        for what is new/grown by ``PrintDone`` — not the other way round.
        """
        try:
            self._print_baseline = self._snapshot_videos()
        except FtpError as exc:
            self._logger.info(
                "print-date: baseline list failed: %s", exc.reason
            )
            self._print_baseline = None

    def _record_print_date(self) -> None:
        when = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        baseline = getattr(self, "_print_baseline", None)
        threading.Thread(
            target=self._record_print_date_worker,
            args=(when, baseline),
            daemon=True,
        ).start()

    def _record_print_date_worker(self, when: str, baseline) -> None:
        # ``baseline`` maps name -> size at PrintStarted (or None if we never
        # got one — fall back to a snapshot taken now). A video that is new,
        # or that grew since the baseline, belongs to this print.
        if baseline is None:
            try:
                baseline = self._snapshot_videos()
            except FtpError as exc:
                self._logger.info(
                    "print-date: initial list failed: %s", exc.reason
                )
                return
        deadline = time.monotonic() + _PRINT_DATE_MAX_WAIT_SECONDS
        while time.monotonic() < deadline:
            try:
                now = self._snapshot_videos()
            except FtpError:
                time.sleep(_PRINT_DATE_POLL_SECONDS)
                continue
            fresh = {
                name
                for name, size in now.items()
                if name not in baseline or size != baseline.get(name)
            }
            if fresh:
                # If several changed at once, stamp them all with the same
                # print-end time — they belong to this print.
                self._store_print_dates(fresh, when)
                self._logger.info(
                    "print-date: stamped %s with %s", sorted(fresh), when
                )
                return
            time.sleep(_PRINT_DATE_POLL_SECONDS)
        self._logger.info("print-date: no new/changed video within window")

    def _snapshot_videos(self) -> dict:
        """Map of SD-card video name -> size."""
        with self._make_ftp() as svc:
            return {f["name"]: f.get("size") for f in svc.list_timelapses()}

    def _store_print_dates(self, names, when: str) -> None:
        dates = dict(self._settings.get(["print_dates"]) or {})
        for name in names:
            dates[name] = when
        self._settings.set(["print_dates"], dates)
        self._settings.save()

    def _autosync_worker(self, delay: int, cancel: threading.Event) -> None:
        """Wait the delay, wait for the idle gate, then pull new timelapses."""
        try:
            if cancel.wait(timeout=delay):
                return
            if not self._wait_until_idle(cancel):
                return
            self._do_autosync()
        except (
            Exception
        ):  # noqa: BLE001 - a background trigger must never crash
            self._logger.exception("auto-sync failed")
        finally:
            with self._autosync_lock:
                if self._autosync_cancel is cancel:
                    self._autosync_cancel = None

    def _gate_open(self) -> bool:
        """True when printer is idle, OctoPrint is not rendering, and no manual
        FTP batch is in flight."""
        return (
            not self._print_active()
            and not self._octoprint_rendering
            and not getattr(self, "_ftp_busy", False)
        )

    def _wait_until_idle(self, cancel: threading.Event) -> bool:
        """Poll the gate until open, cancelled, or the max wait elapses."""
        deadline = time.monotonic() + _GATE_MAX_WAIT_SECONDS
        while not self._gate_open():
            if cancel.wait(timeout=_GATE_POLL_SECONDS):
                return False
            if time.monotonic() > deadline:
                self._logger.info("auto-sync gave up waiting for idle system")
                return False
        return not cancel.is_set()

    def _do_autosync(self) -> None:
        new_names = self._list_new_timelapses()
        if not new_names:
            self._logger.debug("auto-sync: nothing new to pull")
            return
        action = self._settings.get(["auto_sync_action"]) or "copy"
        if action not in ("copy", "move"):
            action = "copy"
        self._logger.info(
            "auto-sync: pulling %d new timelapse(s) via %s",
            len(new_names),
            action,
        )
        self._notify_autosync(len(new_names), action)
        self._run_batch(action, new_names)

    def _list_new_timelapses(self) -> list:
        """Names on the SD card not already present locally.

        We deliberately do NOT use a name-timestamp high-water mark: the
        A1 mini's camera subsystem stamps timelapse videos with a frozen,
        wrong date in LAN-only mode (the printer's main clock is correct, but
        the camera clock is not — verified live, and a known firmware issue:
        forum.bambulab.com timelapse-wrong-date-LAN-only). A newer video can
        therefore carry an *older* name than one already pulled, so a
        timestamp high-water mark would skip it forever. ``_already_copied``
        (does a local file with the canonical name / its .mp4 twin exist?) is
        clock-independent and the only reliable signal.
        """
        try:
            with self._make_ftp() as svc:
                names = [f["name"] for f in svc.list_timelapses()]
        except FtpError as exc:
            self._logger.info("auto-sync list failed: %s", exc.reason)
            return []
        return [name for name in names if not self._already_copied(name)]

    # ------------------------------------------------------------------
    # TEMP measurement harness (plan §10.8 [LATER]: tune auto_sync_delay).
    # Set ``auto_sync_measure`` true, run one print, then read the
    # "render-delay measure" log lines. Remove this block once tuned.
    # ------------------------------------------------------------------
    _MEASURE_POLL_SECONDS = 5
    _MEASURE_MAX_SECONDS = 1800  # give up after 30 min
    # The A1 mini writes the .avi in bursts with multi-second pauses, so a
    # short stable window mistakes a write-pause for "done". Require the size
    # to hold across a long window (6 polls = 30 s) before declaring stable.
    _MEASURE_STABLE_POLLS = 6  # size unchanged across N consecutive polls

    def _maybe_measure_render_delay(self) -> None:  # pragma: no cover
        if not self._settings.get_boolean(["auto_sync_measure"]):
            return
        t0 = time.monotonic()
        threading.Thread(
            target=self._measure_render_delay_worker, args=(t0,), daemon=True
        ).start()

    def _measure_render_delay_worker(  # pragma: no cover
        self, t0: float
    ) -> None:
        log = self._logger
        log.info("render-delay measure: PRINT_DONE at t0; polling SD card")
        known = set()
        try:
            with self._make_ftp() as svc:
                names = {f["name"] for f in svc.list_timelapses()}
            known = names
        except Exception as exc:  # noqa: BLE001 - measurement must not crash
            log.info("render-delay measure: initial list failed: %s", exc)

        appeared = None  # name of the new file
        t_appear = None
        last_size = None
        stable_count = 0
        deadline = t0 + self._MEASURE_MAX_SECONDS

        while time.monotonic() < deadline:
            time.sleep(self._MEASURE_POLL_SECONDS)
            try:
                with self._make_ftp() as svc:
                    files = {
                        f["name"]: f.get("size") for f in svc.list_timelapses()
                    }
            except Exception as exc:  # noqa: BLE001
                log.info("render-delay measure: list failed: %s", exc)
                continue

            if appeared is None:
                # The A1 mini's camera clock jumps between boots, so the new
                # video rarely has the newest *name*. Don't sort by name —
                # take whatever name is not in the PRINT_DONE snapshot. If
                # several appear at once, pick the largest (the just-rendered
                # full video, not a stale leftover).
                fresh = set(files) - known
                if fresh:
                    appeared = max(fresh, key=lambda n: files.get(n) or 0)
                    t_appear = time.monotonic()
                    log.info(
                        "render-delay measure: t1 new file %r APPEARED at "
                        "+%.0fs (all new: %s)",
                        appeared,
                        t_appear - t0,
                        sorted(fresh),
                    )
                continue

            size = files.get(appeared)
            if size != last_size:
                log.info(
                    "render-delay measure: %r size=%s at +%.0fs (growing)",
                    appeared,
                    size,
                    time.monotonic() - t0,
                )
            if size is not None and size == last_size:
                stable_count += 1
                if stable_count >= self._MEASURE_STABLE_POLLS:
                    t_now = time.monotonic()
                    # the size last changed STABLE_POLLS polls ago
                    stable_window = (
                        self._MEASURE_STABLE_POLLS * self._MEASURE_POLL_SECONDS
                    )
                    t_done = t_now - stable_window  # when growth actually ended
                    log.info(
                        "render-delay measure: t2 %r STABLE at size=%s; "
                        "growth ended +%.0fs from PRINT_DONE "
                        "(+%.0fs after appearing). "
                        "Suggested auto_sync_delay >= %d s",
                        appeared,
                        size,
                        t_done - t0,
                        t_done - (t_appear or t0),
                        int(t_done - t0) + 30,  # 30 s safety margin
                    )
                    return
            else:
                stable_count = 0
            last_size = size

        log.info(
            "render-delay measure: gave up after %ds (appeared=%s)",
            self._MEASURE_MAX_SECONDS,
            appeared,
        )

    def _notify_autosync(self, count: int, action: str) -> None:
        self._plugin_manager.send_plugin_message(
            self._identifier,
            {
                "type": "auto_sync",
                "state": "started",
                "count": count,
                "action": action,
            },
        )
