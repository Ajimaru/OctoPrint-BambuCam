"""Background timelapse copy/move/delete and local-``.avi`` conversion.

Split out of the plugin entry point as a mixin so the plugin module stays
within Pylint's module-line cap. ``TimelapseOpsMixin`` is mixed into
``BambucamPlugin`` and relies on attributes/methods the plugin provides
(``_settings``, ``_logger``, ``_plugin_manager``, ``_identifier``,
``_printer``, ``_ftp_lock``, ``_ftp_busy``, ``_make_ftp``).
"""

import logging
import os
import shutil
import threading
import time
from typing import TYPE_CHECKING, Optional

import flask
from octoprint.events import Events, eventManager

from .ftp import FtpError
from .paths import (
    DISK_MARGIN_BYTES,
    FALLBACK_STEM,
    MAX_COLLISION,
    MAX_SUFFIX_LEN,
    sanitize_filename,
)
from .transcode import TimelapseTranscoder, TranscodeError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from octoprint.plugin import PluginSettings
    from octoprint.plugin.core import PluginManager
    from octoprint.printer import PrinterInterface


class TimelapseOpsMixin:
    """Timelapse transfer/convert machinery for ``BambucamPlugin``."""

    _settings: "PluginSettings"
    _plugin_manager: "PluginManager"
    _logger: logging.Logger
    _identifier: str
    _printer: "PrinterInterface"
    _ftp_lock: threading.Lock
    _ftp_busy: bool

    def _make_ftp(self):  # implemented on BambucamPlugin
        raise NotImplementedError

    def _handle_timelapse_op(self, op: str, names: list[str]) -> flask.Response:
        """Validate, guard, and start a background copy/move/delete batch."""
        if not isinstance(names, list) or not names:
            return flask.jsonify(ok=False, reason="bad_name")

        if op in ("move", "delete") and self._print_active():
            return flask.jsonify(ok=False, reason="printing")

        with self._ftp_lock:
            if self._ftp_busy:
                return flask.jsonify(ok=False, reason="busy")
            self._ftp_busy = True

        threading.Thread(
            target=self._run_batch, args=(op, list(names)), daemon=True
        ).start()
        return flask.jsonify(ok=True, queued=len(names))

    def _print_active(self) -> bool:
        printer = getattr(self, "_printer", None)
        if printer is None:
            return False
        return bool(printer.is_printing() or printer.is_paused())

    def _run_batch(self, op: str, names: list[str]) -> None:
        """Process the batch sequentially over a single FTP connection."""
        done = 0
        total = len(names)
        summary = {"copied": 0, "moved": 0, "deleted": 0, "skipped": 0}
        try:
            with self._make_ftp() as svc:
                listing = {f["name"] for f in svc.list_timelapses()}
                for name in names:
                    done += 1
                    batch = {"done": done, "total": total}
                    if name not in listing:
                        summary["skipped"] += 1
                        self._emit_op(
                            op, name, "skipped", batch, reason="bad_name"
                        )
                        continue
                    try:
                        self._process_one(svc, op, name, batch, summary)
                    except FtpError as exc:
                        summary["skipped"] += 1
                        self._emit_op(
                            op, name, "error", batch, reason=exc.reason
                        )
                    except OSError as exc:
                        summary["skipped"] += 1
                        reason = (
                            "no_space"
                            if (getattr(exc, "errno", None) == 28)
                            else "network"
                        )
                        self._emit_op(op, name, "error", batch, reason=reason)
        except FtpError as exc:
            self._emit_op(
                op,
                "",
                "error",
                {"done": done, "total": total},
                reason=exc.reason,
            )
        except Exception:  # noqa: BLE001
            self._logger.exception("timelapse batch failed")
            self._emit_op(
                op, "", "error", {"done": done, "total": total}, reason="error"
            )
        finally:
            with self._ftp_lock:
                self._ftp_busy = False
            self._emit_batch_done(op, summary)

    def _process_one(self, svc, op, name, batch, summary) -> None:
        """Handle a single file for copy/move/delete with all §5.7 guards."""
        if op == "delete":
            svc.delete(name)
            summary["deleted"] += 1
            self._emit_op(op, name, "done", batch)
            return

        remote_size = svc.remote_size(name)
        basefolder = self._settings.global_get_basefolder("timelapse")
        dest = self._build_dest_path(basefolder, name, remote_size)
        if dest is None:
            summary["skipped"] += 1
            self._emit_op(op, name, "error", batch, reason="no_space")
            return
        if isinstance(dest, str) and dest.startswith("@reason:"):
            summary["skipped"] += 1
            self._emit_op(op, name, "error", batch, reason=dest[8:])
            return

        last_pct = [-1]

        def _progress(transferred, total):
            pct = int(transferred * 100 / total) if total else 0

            if pct == last_pct[0]:
                return
            last_pct[0] = pct
            self._emit_op(
                op,
                name,
                "progress",
                batch,
                percent=pct,
                transferred=transferred,
                total=total,
            )

        written = svc.download(name, dest, progress_cb=_progress)

        if op == "move":
            if remote_size is not None and written != remote_size:
                summary["skipped"] += 1
                self._emit_op(op, name, "error", batch, reason="network")
                return
            svc.delete(name)
            summary["moved"] += 1
        else:
            summary["copied"] += 1

        def _on_convert_start():
            self._emit_op(op, name, "converting", batch, percent=0)

        def _on_convert_progress(pct):
            self._emit_op(op, name, "converting", batch, percent=pct)

        final_path, warn = self._maybe_transcode(
            dest,
            on_start=_on_convert_start,
            on_progress=_on_convert_progress,
        )

        # Stamp the local file with the real (host) time. The Bambu camera
        # clock writes the .avi with a wrong date in LAN-only mode, so the
        # name/mtime on the SD are unreliable; the copy happens right after the
        # print, so "now" is the closest correct timestamp and makes the file
        # sort/show correctly in OctoPrint's native Timelapse tab.
        self._stamp_real_mtime(final_path)

        self._fire_movie_done(final_path)
        self._emit_op(
            op,
            name,
            "done",
            batch,
            reason=warn,
            renamed=os.path.basename(final_path),
        )

    def _stamp_real_mtime(self, path) -> None:
        """Set ``path``'s mtime to now (the real copy time).

        Best-effort: a failure here must never fail the copy.
        """
        try:
            now = time.time()
            os.utime(path, (now, now))
        except OSError:
            self._logger.debug("could not stamp mtime on %s", path)

    def _fire_movie_done(self, movie_path) -> None:
        """Fire OctoPrint's ``MovieDone`` event for a file we placed in the
        timelapse folder, so the native Timelapse tab refreshes live (its
        viewmodel calls ``requestData()`` on this event)."""

        note = getattr(self, "note_own_movie", None)
        if callable(note):
            note(movie_path)  # pylint: disable=not-callable
        try:
            eventManager().fire(
                Events.MOVIE_DONE,
                {
                    "gcode": "unknown",
                    "movie": movie_path,
                    "movie_basename": os.path.basename(movie_path),
                    "movie_prefix": os.path.splitext(
                        os.path.basename(movie_path)
                    )[0],
                },
            )
        except Exception:  # noqa: BLE001 - never let an event break the batch
            self._logger.debug("could not fire MovieDone", exc_info=True)

    def _make_transcoder(self) -> TimelapseTranscoder:
        """Build a transcoder from OctoPrint's own webcam ffmpeg config.

        Reuses ``webcam.ffmpeg`` (the binary OctoPrint already located for its
        native timelapse rendering) plus its codec/bitrate/threads so we don't
        add a second ffmpeg path or probe.
        """
        return TimelapseTranscoder(
            self._logger,
            ffmpeg_path=self._settings.global_get(["webcam", "ffmpeg"]),
            videocodec=self._settings.global_get(
                ["webcam", "ffmpegVideoCodec"]
            ),
            bitrate=self._settings.global_get(["webcam", "bitrate"]),
            threads=self._settings.global_get_int(["webcam", "ffmpegThreads"]),
            thumbnail_commandline=self._settings.global_get(
                ["webcam", "ffmpegThumbnailCommandline"]
            ),
        )

    def _maybe_transcode(self, avi_path, *, on_start=None, on_progress=None):
        """Re-encode a downloaded ``.avi`` to ``.mp4`` and drop the ``.avi``.

        Returns ``(final_path, warning)``: on success ``final_path`` is the new
        ``.mp4`` and ``warning`` is ``None``; on any skip/failure the ``.avi``
        is kept and ``final_path`` is the unchanged ``.avi`` with a short
        ``warning`` reason (``transcode_failed`` / ``no_ffmpeg`` /
        ``printing``). Never raises — a transcode problem must not fail an
        otherwise-good copy. ``on_start`` is called once right before ffmpeg
        runs; ``on_progress(percent)`` as ffmpeg reports progress.
        """
        if not avi_path.lower().endswith(".avi"):
            return avi_path, None
        if not self._settings.get_boolean(["transcode_to_mp4"]):
            return avi_path, None

        transcoder = self._make_transcoder()
        if not transcoder.available():
            return avi_path, "no_ffmpeg"
        if self._print_active():
            return avi_path, "printing"

        mp4_path = avi_path[: -len(".avi")] + ".mp4"
        mp4_path = self._collision_safe_path(mp4_path)
        if on_start is not None:
            on_start()
        try:
            transcoder.transcode(avi_path, mp4_path, progress_cb=on_progress)
        except TranscodeError as exc:
            self._logger.warning("transcode failed (%s): %s", exc.reason, exc)
            return avi_path, "transcode_failed"
        transcoder.create_thumbnail(mp4_path, mp4_path + ".thumb.jpg")
        try:
            os.remove(avi_path)
        except OSError:
            self._logger.warning(
                "could not remove %s after transcode", avi_path
            )
        return mp4_path, None

    @staticmethod
    def _collision_safe_path(path):
        """Append ``-N`` to ``path``'s stem until it is free (cap 1000)."""
        if not os.path.exists(path):
            return path
        stem, ext = os.path.splitext(path)
        for i in range(1, MAX_COLLISION + 1):
            cand = f"{stem}-{i}{ext}"
            if not os.path.exists(cand):
                return cand
        return path

    def _list_local_avi(self) -> list:
        """List ``.avi`` files in the OctoPrint timelapse folder.

        These are downloads whose transcode was off/skipped/failed. Returns
        ``[{name, size}]``; an empty list is normal.
        """
        basefolder = self._settings.global_get_basefolder("timelapse")
        files = []
        try:
            entries = os.listdir(basefolder)
        except OSError:
            return []
        for entry in sorted(entries):
            if not entry.lower().endswith(".avi"):
                continue
            path = os.path.join(basefolder, entry)
            if not os.path.isfile(path):
                continue
            files.append({"name": entry, "size": os.path.getsize(path)})
        return files

    def _handle_convert_local(self, names) -> flask.Response:
        """Validate, guard and start a background local-``.avi`` conversion."""
        if not isinstance(names, list) or not names:
            return flask.jsonify(ok=False, reason="bad_name")
        if self._print_active():
            return flask.jsonify(ok=False, reason="printing")
        transcoder = self._make_transcoder()
        if not transcoder.available():
            return flask.jsonify(ok=False, reason="no_ffmpeg")

        with self._ftp_lock:
            if self._ftp_busy:
                return flask.jsonify(ok=False, reason="busy")
            self._ftp_busy = True

        threading.Thread(
            target=self._run_convert_batch,
            args=(list(names),),
            daemon=True,
        ).start()
        return flask.jsonify(ok=True, queued=len(names))

    def _run_convert_batch(self, names) -> None:
        """Convert each local ``.avi`` to ``.mp4`` sequentially (CPU-bound)."""
        basefolder = self._settings.global_get_basefolder("timelapse")
        known = {f["name"] for f in self._list_local_avi()}
        transcoder = self._make_transcoder()
        done = 0
        total = len(names)
        summary = {"converted": 0, "skipped": 0}
        try:
            for name in names:
                done += 1
                batch = {"done": done, "total": total}
                safe = sanitize_filename(os.path.basename(name))
                if not safe or safe not in known:
                    summary["skipped"] += 1
                    self._emit_convert(
                        name, "skipped", batch, reason="bad_name"
                    )
                    continue
                avi_path = os.path.join(basefolder, safe)
                if not self._is_contained(avi_path, basefolder):
                    summary["skipped"] += 1
                    self._emit_convert(
                        name, "skipped", batch, reason="bad_name"
                    )
                    continue
                self._convert_one(transcoder, name, avi_path, batch, summary)
        except Exception:  # noqa: BLE001
            self._logger.exception("convert batch failed")
        finally:
            with self._ftp_lock:
                self._ftp_busy = False
            self._plugin_manager.send_plugin_message(
                self._identifier,
                {
                    "type": "convert_op",
                    "state": "batch_done",
                    "summary": summary,
                },
            )

    def _convert_one(self, transcoder, name, avi_path, batch, summary) -> None:
        mp4_path = self._collision_safe_path(avi_path[: -len(".avi")] + ".mp4")

        def _progress(pct):
            self._emit_convert(name, "progress", batch, percent=pct)

        try:
            transcoder.transcode(avi_path, mp4_path, progress_cb=_progress)
        except TranscodeError as exc:
            self._logger.warning("convert failed (%s): %s", exc.reason, exc)
            summary["skipped"] += 1
            self._emit_convert(name, "error", batch, reason="transcode_failed")
            return
        transcoder.create_thumbnail(mp4_path, mp4_path + ".thumb.jpg")
        try:
            os.remove(avi_path)
        except OSError:
            self._logger.warning("could not remove %s", avi_path)
        self._fire_movie_done(mp4_path)
        summary["converted"] += 1
        self._emit_convert(
            name, "done", batch, renamed=os.path.basename(mp4_path)
        )

    def _emit_convert(
        self, name, state, batch, *, reason=None, renamed=None, percent=None
    ):
        msg = {
            "type": "convert_op",
            "name": name,
            "state": state,
            "batch": batch,
        }
        if reason is not None:
            msg["reason"] = reason
        if renamed is not None:
            msg["renamed"] = renamed
        if percent is not None:
            msg["percent"] = percent
        self._plugin_manager.send_plugin_message(self._identifier, msg)

    def _build_dest_path(self, basefolder, name, remote_size):
        """Build a safe, contained, free-space-checked destination path.

        Returns the path string, or ``None`` for ``no_space``, or
        ``"@reason:<reason>"`` for ``bad_name`` / ``name_conflict``. Enforces
        plan §5.7 (sanitize, fallback, suffix cap, collision cap, containment,
        disk space). Does NOT create the file.
        """
        candidate = self._canonical_local_name(name)
        cand_stem, cand_ext = os.path.splitext(candidate)

        if remote_size is not None:
            free = shutil.disk_usage(basefolder).free
            if free <= remote_size * 1.1 + DISK_MARGIN_BYTES:
                return None

        final = self._collision_safe(basefolder, cand_stem, cand_ext)
        if final is None:
            return "@reason:name_conflict"
        dest = os.path.join(basefolder, final)
        if not self._is_contained(dest, basefolder):
            return "@reason:bad_name"
        return dest

    def _sanitized_suffix(self) -> str:
        raw = self._settings.get(["download_suffix"]) or ""
        cleaned = sanitize_filename(raw).replace(".", "")
        if cleaned in ("", ".", ".."):
            return ""
        return cleaned[:MAX_SUFFIX_LEN]

    @staticmethod
    def _collision_safe(basefolder, stem, ext) -> Optional[str]:
        """Return a non-clobbering filename, appending ``-N`` if needed."""
        name = f"{stem}{ext}"
        if not os.path.exists(os.path.join(basefolder, name)):
            return name
        for i in range(1, MAX_COLLISION + 1):
            name = f"{stem}-{i}{ext}"
            if not os.path.exists(os.path.join(basefolder, name)):
                return name
        return None

    @staticmethod
    def _is_contained(dest, basefolder) -> bool:
        base = os.path.realpath(basefolder)
        return os.path.realpath(dest).startswith(base + os.sep)

    def _canonical_local_name(self, name) -> str:
        """The §5.8 canonical local name (no ``-N`` collision counter).

        Builds ``<stem><suffix><ext>``, sanitizes it, and substitutes a safe
        fallback stem if sanitizing leaves nothing usable (§5.7 #6).
        """
        base = os.path.basename(name)
        if base.startswith(".") and base.count(".") == 1:
            stem, ext = "", base
        else:
            stem, ext = os.path.splitext(base)
        suffix = self._sanitized_suffix()
        if not stem:
            stem = FALLBACK_STEM
        candidate = sanitize_filename(f"{stem}{suffix}{ext}")
        cand_stem, _ = os.path.splitext(candidate)
        if not candidate or not cand_stem:
            candidate = sanitize_filename(f"{FALLBACK_STEM}{suffix}{ext}")
        return candidate

    def _local_copy_name(self, name) -> Optional[str]:
        """The local filename this SD video was copied to, or ``None``.

        Prefers the transcoded ``.mp4`` twin (the ``.avi`` is removed after a
        successful transcode), falling back to the canonical ``.avi`` when the
        transcode was off/failed. Used both to flag a row as copied and to show
        the user which local file it became (e.g. the ``→ name.mp4`` hint).
        """
        basefolder = self._settings.global_get_basefolder("timelapse")
        canonical = self._canonical_local_name(name)
        if canonical.lower().endswith(".avi"):
            mp4 = canonical[: -len(".avi")] + ".mp4"
            if os.path.exists(os.path.join(basefolder, mp4)):
                return mp4
        if os.path.exists(os.path.join(basefolder, canonical)):
            return canonical
        return None

    def _already_copied(self, name) -> bool:
        """True when the canonical local file already exists (§5.8).

        For an ``.avi`` that transcoding turns into ``.mp4``, a match on either
        the canonical ``.avi`` (transcode off/failed) or its ``.mp4`` twin
        (transcode succeeded, ``.avi`` removed) counts as copied.
        """
        return self._local_copy_name(name) is not None

    def _emit_op(
        self,
        op,
        name,
        state,
        batch,
        *,
        percent=None,
        reason=None,
        renamed=None,
        transferred=None,
        total=None,
    ):
        msg = {
            "type": "timelapse_op",
            "op": op,
            "name": name,
            "state": state,
            "batch": batch,
        }
        if percent is not None:
            msg["percent"] = percent
        if reason is not None:
            msg["reason"] = reason
        if renamed is not None:
            msg["renamed"] = renamed
        if transferred is not None:
            msg["transferred"] = transferred
        if total is not None:
            msg["total"] = total
        self._plugin_manager.send_plugin_message(self._identifier, msg)

    def _emit_batch_done(self, op, summary):
        self._plugin_manager.send_plugin_message(
            self._identifier,
            {
                "type": "timelapse_op",
                "op": op,
                "state": "batch_done",
                "summary": summary,
            },
        )
