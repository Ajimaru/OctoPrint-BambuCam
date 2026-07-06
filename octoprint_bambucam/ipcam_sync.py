"""Auto-download a print's ``/ipcam`` chunks when it finishes (plan §3.2).

When ``auto_download_ipcam`` is on, ``PrintStarted`` snapshots ``/ipcam`` as a
baseline and ``PrintDone`` (after the ring-buffer delay) diffs it to find this
print's new/grown chunks, then downloads them raw into
``raw/chunks/<print-id>/`` with an ``order.json`` (concat order + per-chunk
``included`` default). No concat/render happens here — the user triggers that in
the Raw Files tab, or ``auto_render_new_groups`` queues it automatically after
a complete download. A partial/failed download leaves the group ``incomplete``
and pushes a toast so the user can re-harvest inside the ~24 h ring-buffer
window.

``IpcamSyncMixin`` is mixed into ``BambucamPlugin`` and reuses the autosync
gate/cancel patterns plus the render paths/library the plugin builds.
"""

import datetime
import ftplib  # nosec B402 - error classes only; transfers run over FTPS
import json
import logging
import os
import shutil
import threading
from typing import TYPE_CHECKING, Callable, Optional

from octoprint.events import Events

from . import render_paths
from .ftp import FtpError
from .ipcam_ftp import BambuIpcamFtp
from .paths import DISK_MARGIN_BYTES

if TYPE_CHECKING:  # pragma: no cover - typing only
    from octoprint.plugin import PluginSettings
    from octoprint.plugin.core import PluginManager

    from .render_paths import RenderPaths

_RETRY_BACKOFFS = (5, 15, 45)
_GATE_POLL_SECONDS = 5
_GATE_MAX_WAIT_SECONDS = 1800

# Without a PrintStarted baseline, a diff against an empty set would treat the
# *entire* ~160-slot ring buffer (~17 GB) as this print's footage. Cap the
# no-baseline harvest to the newest few chunks (by FTP modify time) so a manual
# "Scan /ipcam now" or a missed baseline never floods the Pi's disk.
_NO_BASELINE_MAX_CHUNKS = 6


class _HarvestCancelled(Exception):
    """Raised from the download progress callback to abort a chunk transfer."""


class IpcamSyncMixin:
    """Harvest a print's ``/ipcam`` chunks on completion."""

    _settings: "PluginSettings"
    _plugin_manager: "PluginManager"
    _logger: logging.Logger
    _identifier: str

    # ``_render_paths``/``_auto_render_group`` (RawFilesOpsMixin),
    # ``_make_ipcam_ftp`` (plugin) and ``_print_active`` (TimelapseOpsMixin)
    # come from sibling mixins on ``BambucamPlugin`` and are not redeclared at
    # runtime, so the MRO resolves to the concrete implementations. They are
    # declared as callable-typed attributes (no runtime body) so both Pylance
    # and Pylint see their signatures without shadowing the real methods.
    _render_paths: "Callable[[], RenderPaths]"
    _auto_render_group: "Callable[[str], None]"
    # ``_pipeline_chunk_progress`` (AutoSyncMixin) mirrors harvest progress to
    # the Raw Files tab's bar. Declared callable-typed for the same MRO reason.
    _pipeline_chunk_progress: "Callable[[int, int], None]"
    # ``_pipeline_download_progress`` (AutoSyncMixin) mirrors the live
    # download speed of the chunk currently transferring. Same MRO reason.
    _pipeline_download_progress: "Callable[[int, Optional[int]], None]"
    # ``_manual_harvest_ui`` (AutoSyncMixin) is a context manager that always
    # lowers the UI busy flag when a manual harvest ends. Callable-typed.
    _manual_harvest_ui: Callable
    # ``_webcam_paused_for_harvest`` (plugin) is a context manager that stops
    # the live stream during the pull so it doesn't compete for the printer's
    # slow FTPS link. Callable-typed for the same MRO reason as the above.
    _webcam_paused_for_harvest: Callable

    def _make_ipcam_ftp(self) -> BambuIpcamFtp:  # pragma: no cover - on plugin
        raise NotImplementedError

    def _init_ipcam_sync(self) -> None:
        """Initialize ipcam-sync state. Call from the plugin ``__init__``."""
        self._ipcam_lock = threading.Lock()
        self._ipcam_baseline: Optional[dict] = None
        self._ipcam_cancel: Optional[threading.Event] = None
        # Serializes overlapping manual harvests so they queue back-to-back
        # instead of cancelling each other (see ``harvest_now``).
        self._ipcam_harvest_lock = threading.Lock()

    def on_ipcam_event(self, event, _payload) -> None:
        """Snapshot the ``/ipcam`` baseline when a print starts.

        Called from the plugin's ``on_event``. Only ``PRINT_STARTED`` is
        handled here: it records the ring-buffer baseline so ``PRINT_DONE`` can
        diff against it. The harvest itself no longer runs as its own delayed
        thread — it is **stage 3** of the serial post-print pipeline in
        ``AutoSyncMixin`` (``_run_ipcam_harvest``), so it can never run
        concurrently with the SD-card copy (a second FTPS session yields
        ``425``). A duplicate ``PRINT_STARTED`` (observed on the A1 mini) just
        re-snapshots the baseline, which is harmless since ``/ipcam`` barely
        changes in that window.
        """
        if not self._settings.get_boolean(["auto_download_ipcam"]):
            return
        if event == Events.PRINT_STARTED:
            threading.Thread(
                target=self._snapshot_ipcam_baseline, daemon=True
            ).start()

    def _run_ipcam_harvest(self, payload, cancel) -> None:
        """Stage 3 of the post-print pipeline: diff ``/ipcam`` and download.

        Runs **synchronously** on the pipeline worker — no own thread and no
        own delay, because the pipeline already waited the ring-buffer delay
        and the idle gate, and stage 2 (the SD copy) has finished. Diffs the
        print-start baseline, then downloads this print's new chunks. A missing
        baseline falls back to the newest-chunks cap inside ``_diff_ipcam``.
        """
        self._harvest(
            when=datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
            gcode=_gcode_name(payload),
            baseline=self._ipcam_baseline,
            cancel=cancel,
        )

    def _harvest(self, when, gcode, baseline, cancel) -> None:
        """Diff ``/ipcam`` against ``baseline``; download this print's chunks.

        Shared by the post-print pipeline (real ``PRINT_STARTED`` baseline) and
        the manual "Scan /ipcam now" fallback (``baseline=None`` → newest-chunks
        cap). Logs the outcome at INFO so every harvest is traceable.
        """
        chunks = self._diff_ipcam(baseline)
        if not chunks:
            self._logger.info("ipcam harvest: no new chunks")
            # Emit a terminal push so a client that showed a "fetching" toast
            # for a manual harvest can drop it — there is simply nothing new.
            self._notify_download("", "done", count=0)
            return
        print_id = self._unique_print_id(when, gcode)
        self._logger.info(
            "ipcam harvest: %d new chunk(s) -> %s", len(chunks), print_id
        )
        self._download_group(print_id, chunks, cancel)

    def _snapshot_ipcam_baseline(self) -> None:
        try:
            self._ipcam_baseline = self._snapshot_ipcam()
        except FtpError as exc:
            self._logger.info("ipcam baseline failed: %s", exc.reason)
            self._ipcam_baseline = None

    def _snapshot_ipcam(self) -> dict:
        """Map of ``/ipcam`` chunk name -> ``{size, mdtm, slot}``."""
        with self._make_ipcam_ftp() as svc:
            return {
                f["name"]: {
                    "size": f.get("size"),
                    "mdtm": f.get("date"),
                    "slot": f.get("slot"),
                }
                for f in svc.list_ipcam()
            }

    def _diff_ipcam(self, baseline) -> list:
        """Return this print's chunks: new or grown since the baseline.

        With a real ``PrintStarted`` baseline the diff is exact. **Without** one
        (a missed baseline or a manual harvest), a naive diff against an empty
        set would pull the whole ring buffer (~17 GB), so the result is instead
        capped to the newest :data:`_NO_BASELINE_MAX_CHUNKS` chunks by FTP
        modify time. Sorted by slot then name for a stable concat order.
        """
        try:
            current = self._snapshot_ipcam()
        except FtpError as exc:
            self._logger.info("ipcam diff list failed: %s", exc.reason)
            return []
        if baseline is None:
            return self._newest_chunks(current)
        fresh = []
        for name, info in current.items():
            old = baseline.get(name)
            if old is None or old.get("size") != info.get("size"):
                fresh.append({"name": name, **info})
        fresh.sort(key=lambda c: (c.get("slot") or 0, c["name"]))
        return fresh

    @staticmethod
    def _newest_chunks(current: dict) -> list:
        """Pick the newest few chunks by MDTM (no-baseline safety cap)."""
        items = [{"name": name, **info} for name, info in current.items()]
        items.sort(key=lambda c: (c.get("mdtm") or "", c["name"]), reverse=True)
        newest = items[:_NO_BASELINE_MAX_CHUNKS]
        newest.sort(key=lambda c: (c.get("slot") or 0, c["name"]))
        return newest

    def _unique_print_id(self, when, gcode) -> str:
        """Build a print-id and disambiguate same-minute collisions (``-N``)."""
        base = render_paths.build_print_id(when, gcode)
        paths = self._render_paths()
        candidate = base
        suffix = 1
        while True:
            group = paths.group_dir(candidate)
            if group is not None and not os.path.exists(group):
                return candidate
            candidate = f"{base}-{suffix}"
            suffix += 1
            if suffix > 1000:
                return base

    def _download_group(self, print_id, chunks, cancel) -> None:
        """Download all chunks into the group dir, writing ``order.json``.

        Pushes ``started``/``done``/``failed`` to the UI. A disk-space shortfall
        or a chunk that fails after retries leaves the group ``incomplete`` and
        emits a failure toast so the user can re-harvest. A complete download
        hands the group to the auto-render hook (``auto_render_new_groups``).
        """
        paths = self._render_paths()
        group = paths.group_dir(print_id)
        if group is None:
            # An unsafe/invalid print-id can't be contained; surface a terminal
            # failure so a client "fetching" toast doesn't hang forever.
            self._notify_download(print_id, "failed", reason="incomplete")
            return
        os.makedirs(group, exist_ok=True)
        _clear_part_files(group)
        self._notify_download(print_id, "started", count=len(chunks))
        if not self._has_space_for(group, chunks):
            self._notify_download(print_id, "failed", reason="no_space")
            _discard_empty_group(group)
            return
        # Pause the live stream for the pull so it doesn't compete for the
        # printer's slow FTPS link (the stream is auto-resumed afterwards).
        with self._webcam_paused_for_harvest():
            order, failed = self._pull_chunks(group, chunks, cancel)
        # A run that saved nothing (all chunks failed, cancelled before the
        # first byte, or a fully partial harvest) must not leave a phantom
        # group behind: no order.json, remove the empty dir. Otherwise the Raw
        # Files tab would list a 0-chunk "broken" group the user has to clean
        # up by hand (observed on the A1 mini's overlapping manual harvests).
        if not order:
            _discard_empty_group(group)
            reason = "cancelled" if cancel.is_set() else "incomplete"
            self._notify_download(
                print_id, "failed", reason=reason, got=0, want=len(chunks)
            )
            return
        self._write_order(print_id, order)
        if cancel.is_set():
            self._notify_download(
                print_id,
                "failed",
                reason="cancelled",
                got=len(order),
                want=len(chunks),
            )
        elif failed:
            self._notify_download(
                print_id,
                "failed",
                reason="incomplete",
                got=len(order),
                want=len(chunks),
            )
        else:
            self._notify_download(print_id, "done", count=len(order))
            self._auto_render_group(print_id)

    def _pull_chunks(self, group, chunks, cancel) -> tuple:
        """Download each chunk with retry/backoff; return (order, failed).

        Reports ``done/total`` chunk progress to the UI before each download so
        the Raw Files tab's harvest bar advances. Progress is best-effort — a
        missing mirror hook (e.g. in unit tests) must not break a harvest.
        """
        order = []
        failed = False
        total = len(chunks)
        for index, chunk in enumerate(chunks):
            if cancel.is_set():
                failed = True
                break
            self._report_chunk_progress(index, total)
            name = chunk["name"]
            dest = os.path.join(group, name)
            if self._download_one(name, dest, cancel):
                order.append(
                    {
                        "file": name,
                        "slot": chunk.get("slot"),
                        "mdtm": chunk.get("mdtm"),
                        "included": True,
                    }
                )
            else:
                failed = True
        self._report_chunk_progress(len(order), total)
        return order, failed

    def _report_chunk_progress(self, done: int, total: int) -> None:
        try:
            self._pipeline_chunk_progress(done, total)
        except Exception:  # noqa: BLE001 - progress is cosmetic
            pass

    def _report_download_progress(self, transferred: int, total) -> None:
        try:
            self._pipeline_download_progress(transferred, total)
        except Exception:  # noqa: BLE001 - speed readout is cosmetic
            pass

    def _download_one(self, name, dest, cancel) -> bool:
        """Download one chunk, retrying with backoff (plan §Opt. 6).

        A ``progress_cb`` that raises on ``cancel`` aborts the (potentially
        very long, ~129 MB) FTP transfer promptly when a newer print/harvest
        takes priority; ``download`` then removes its ``.part`` temp on the
        raised exception, so no half-file is left behind.
        """

        def _abort_if_cancelled(transferred, total):
            if cancel.is_set():
                raise _HarvestCancelled()
            self._report_download_progress(transferred, total)

        for attempt, backoff in enumerate((0, *_RETRY_BACKOFFS)):
            if cancel.wait(timeout=backoff):
                return False
            try:
                with self._make_ipcam_ftp() as svc:
                    svc.download(name, dest, progress_cb=_abort_if_cancelled)
                return True
            except _HarvestCancelled:
                return False
            except (FtpError, OSError, *ftplib.all_errors) as exc:
                self._logger.info(
                    "ipcam chunk %s attempt %d failed: %s",
                    name,
                    attempt + 1,
                    exc,
                )
        return False

    def _has_space_for(self, group, chunks) -> bool:
        """Check the disk has room for the chunk total plus the safety margin.

        Uses the summed remote sizes when known (the ``/ipcam`` snapshot carries
        them); an unknown total skips the check rather than blocking a harvest.
        """
        total = sum(c.get("size") or 0 for c in chunks)
        if total <= 0:
            return True
        try:
            free = shutil.disk_usage(group).free
        except OSError:
            return True
        return free > total * 1.1 + DISK_MARGIN_BYTES

    def _write_order(self, print_id, order) -> None:
        """Atomically write ``order.json`` for the group."""
        order_file = self._render_paths().order_file(print_id)
        if order_file is None:
            return
        tmp = order_file + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(order, fh)
            os.replace(tmp, order_file)
        except OSError:
            self._logger.warning("could not write order.json for %s", print_id)

    def harvest_now(self, when=None, gcode=None) -> dict:
        """Manual ``/ipcam`` harvest fallback (the "Fetch from printer" button).

        Diffs ``/ipcam`` against an empty baseline (everything currently on the
        card for the most recent print) and downloads it on a worker thread.

        A manual harvest no longer **cancels** an in-flight one — that was what
        produced abandoned ``.part`` files and phantom groups when a user
        clicked while the post-print harvest was still running. Instead each
        manual harvest **queues** behind the ``_ipcam_harvest_lock`` and runs
        only when the previous one (and, via the FTP session lock, any pipeline
        harvest) has finished. The gcode name falls back to the most recent
        recorded print job so a manual pull is labelled with the real print
        instead of ``unknown-print``. Returns ``{ok}`` immediately.
        """
        when = when or datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        gcode = gcode or self._last_print_job()
        cancel = threading.Event()

        def _worker() -> None:
            # Serialize manual harvests so overlapping clicks run back-to-back
            # rather than cancelling each other mid-download. The UI context
            # manager always lowers the busy flag when the harvest ends, so the
            # Raw Files bar and the held Timelapse refresh are released even
            # though this path never runs through _post_print_worker.
            with self._ipcam_harvest_lock, self._manual_harvest_ui():
                try:
                    self._harvest(when, gcode, None, cancel)
                except Exception:  # noqa: BLE001 - must not crash trigger
                    self._logger.exception("manual ipcam harvest failed")

        threading.Thread(target=_worker, daemon=True).start()
        return {"ok": True}

    def _last_print_job(self) -> Optional[str]:
        """Most recently recorded print job name (for manual-harvest labels).

        ``print_jobs`` maps SD video name -> gcode name; the newest is keyed by
        the matching ``print_dates`` entry. Returns ``None`` when nothing has
        been recorded yet, so the harvest falls back to its own default stem.
        """
        jobs = self._settings.get(["print_jobs"]) or {}
        dates = self._settings.get(["print_dates"]) or {}
        if not jobs:
            return None
        best_name = max(
            jobs, key=lambda name: dates.get(name, ""), default=None
        )
        return jobs.get(best_name) if best_name else None

    def _notify_download(self, print_id, state, **extra) -> None:
        msg = {"type": "ipcam_download", "print_id": print_id, "state": state}
        msg.update(extra)
        self._plugin_manager.send_plugin_message(self._identifier, msg)


def _gcode_name(payload) -> Optional[str]:
    """Extract the gcode file name from a PrintDone payload."""
    if not isinstance(payload, dict):
        return None
    return payload.get("name") or payload.get("path")


def _clear_part_files(group: str) -> None:
    """Remove leftover ``*.part`` temps from an interrupted prior harvest.

    A cancelled/crashed download leaves a partial ``.part`` in the group; a
    fresh harvest of the same group starts clean rather than mistaking it for a
    finished chunk.
    """
    try:
        entries = os.listdir(group)
    except OSError:
        return
    for entry in entries:
        if entry.endswith(".part"):
            try:
                os.remove(os.path.join(group, entry))
            except OSError:
                pass


def _discard_empty_group(group: str) -> None:
    """Delete a group dir that ended up with no saved chunks.

    Clears any ``.part`` remnants first, then removes the directory. Best-effort
    (ignore_errors): a leftover phantom group is a cosmetic annoyance, not worth
    crashing a background harvest over.
    """
    _clear_part_files(group)
    shutil.rmtree(group, ignore_errors=True)
