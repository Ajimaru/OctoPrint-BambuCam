"""API handlers and wiring for the Raw Files render tab (plan §3.7).

``RawFilesOpsMixin`` is mixed into ``BambucamPlugin`` and exposes the render
pipeline to the frontend: scanning the footage library, harvesting ``/ipcam``,
queueing/cancelling renders, deleting groups, and reporting queue/ffprobe
status. It also builds the raw-preview thumbnail (the last frame of a group's
last chunk) and serves it.

The mixin lazily constructs the pipeline objects (paths, ffprobe, library,
queue, worker) the first time they are needed and caches them, so the plugin
entry point stays small and the pipeline only spins up when the render feature
is used.
"""

import json
import logging
import os
import re
import shutil
import subprocess  # nosec B404 - fixed argv list, no shell; path from settings
import threading
import time
from typing import TYPE_CHECKING, Callable, Optional

import flask
from octoprint.util import RepeatedTimer

from . import render_presets
from .ffprobe import FfprobeRunner, fallback_ffprobe_path
from .gcode_thumb import gcode_thumb_source_by_stem
from .raw_library import STATE_CHUNKS_READY, RawLibrary
from .render_paths import RenderPaths, is_contained, is_valid_print_id
from .render_queue import RenderQueue
from .render_registry import JobRegistry
from .render_worker import RenderOptions, RenderWorker
from .transcode import TRANSCODE_TIMEOUT

if TYPE_CHECKING:  # pragma: no cover - typing only
    from octoprint.plugin import PluginSettings
    from octoprint.plugin.core import PluginManager

THUMB_TIMEOUT = 30
RETENTION_SWEEP_SECONDS = 12 * 3600


class RawFilesOpsMixin:
    """Render-pipeline API surface for ``BambucamPlugin``."""

    _settings: "PluginSettings"
    _plugin_manager: "PluginManager"
    _logger: logging.Logger
    _identifier: str

    # ``get_plugin_data_folder`` (OctoPrint base), ``_print_active`` and
    # ``_fire_movie_done`` (TimelapseOpsMixin) come from sibling base classes on
    # ``BambucamPlugin``. They are declared as callable-typed attributes (no
    # runtime body) so the type checker sees their signatures without shadowing
    # the real implementations, which the MRO still resolves to.
    get_plugin_data_folder: "Callable[[], str]"
    _print_active: "Callable[[], bool]"
    _fire_movie_done: "Callable[[str], None]"
    _sanitized_suffix: "Callable[[], str]"

    def _init_raw_files(self) -> None:
        """Initialize lazy-pipeline state. Call from the plugin ``__init__``."""
        self._render_paths_obj: Optional[RenderPaths] = None
        self._raw_library: Optional[RawLibrary] = None
        self._render_queue: Optional[RenderQueue] = None
        self._render_lock = threading.Lock()
        self._retention_timer: Optional[RepeatedTimer] = None

    # ------------------------------------------------------------------
    # Lazy pipeline construction
    # ------------------------------------------------------------------
    def _render_paths(self) -> RenderPaths:
        if self._render_paths_obj is None:
            paths = RenderPaths(self.get_plugin_data_folder())
            paths.ensure_dirs()
            self._render_paths_obj = paths
        return self._render_paths_obj

    def _ffprobe_runner(self) -> FfprobeRunner:
        path = (self._settings.get(["ffprobe_path"]) or "").strip()
        if not path:
            path = (
                fallback_ffprobe_path(
                    self._settings.global_get(["webcam", "ffmpeg"])
                )
                or ""
            )
        return FfprobeRunner(self._logger, ffprobe_path=path)

    def _ffmpeg_path(self) -> str:
        path = (self._settings.get(["ffmpeg_path"]) or "").strip()
        if path:
            return path
        return self._settings.global_get(["webcam", "ffmpeg"]) or ""

    def _ffmpeg_threads(self) -> int:
        """ffmpeg ``-threads`` value from settings.

        ``0`` means "let ffmpeg use all cores" and must be preserved — a plain
        ``get_int(...) or 1`` would turn the intended 0 into 1, capping the
        encode to a single core. Only a missing/negative value falls back to 1.
        """
        threads = self._settings.get_int(["ffmpeg_threads"])
        if threads is None or threads < 0:
            return 1
        return threads

    def _library(self) -> RawLibrary:
        if self._raw_library is None:
            self._raw_library = RawLibrary(
                self._logger, self._render_paths(), self._ffprobe_runner()
            )
        return self._raw_library

    def _queue(self) -> RenderQueue:
        if self._render_queue is None:
            registry = JobRegistry(self._logger, self._render_paths().jobs_file)
            registry.load()
            self._render_queue = RenderQueue(
                self._logger,
                self._render_paths(),
                registry,
                run_job=self._run_render_job,
                gate_open=self._render_gate_open,
                notify=self._notify_render_job,
                on_group_rendered=self._on_group_rendered,
                max_queue_size=self._settings.get_int(["max_queue_size"]) or 10,
            )
            self._render_queue.start()
        return self._render_queue

    def start_render_pipeline(self) -> None:
        """Build the queue and run startup recovery. Call on_after_startup."""
        if not self._settings.get_boolean(["render_enabled"]):
            return
        timeout = self._settings.get_int(["stale_lock_timeout"]) or 86400
        self._queue().recover(timeout)
        self._library().scan()
        self._start_retention_timer()

    def stop_render_pipeline(self) -> None:
        """Stop the render worker and retention timer if they were started."""
        queue = getattr(self, "_render_queue", None)
        if queue is not None:
            queue.stop()
        timer = getattr(self, "_retention_timer", None)
        if timer is not None:
            timer.cancel()
            self._retention_timer = None

    # ------------------------------------------------------------------
    # Retention: age out rendered groups and old trash
    # ------------------------------------------------------------------
    def _start_retention_timer(self) -> None:
        """Run the retention sweep now and then every 12 h."""
        self._retention_cleanup()
        timer = RepeatedTimer(RETENTION_SWEEP_SECONDS, self._retention_sweep)
        timer.start()
        self._retention_timer = timer

    def _retention_sweep(self) -> None:
        try:
            self._retention_cleanup()
        # a timer tick must never crash
        except Exception:  # noqa: BLE001  # pylint: disable=broad-except
            self._logger.exception("retention sweep failed")

    def _retention_cleanup(self) -> None:
        """Age out rendered groups and purge old trash entries.

        ``chunks_retention_days`` (0 = keep forever) bounds how long a
        *rendered* group's chunks stay around; the age is the
        ``rendered.json`` marker's mtime, so re-rendering resets the clock.
        Expired groups are soft-deleted to ``trash/`` (or removed outright
        when ``move_to_trash`` is off), and trash entries older than the same
        window are purged for good. Unrendered groups are never touched.
        """
        days = self._settings.get_int(["chunks_retention_days"]) or 0
        if days <= 0:
            return
        cutoff = time.time() - days * 86400
        paths = self._render_paths()
        to_trash = self._settings.get_boolean(["move_to_trash"])
        for print_id in _listdir_safe(paths.raw_chunks_dir):
            marker = paths.rendered_marker(print_id)
            if marker is None or not os.path.isfile(marker):
                continue
            if os.path.getmtime(marker) >= cutoff:
                continue
            group = paths.group_dir(print_id)
            # re-assert containment at the sink (group is already vetted by
            # group_dir; this guards against future callers and makes the
            # sanitization visible to taint analysis)
            if group is None or not is_contained(group, paths.raw_chunks_dir):
                continue
            if to_trash:
                removed = self._trash_group(print_id)
            else:
                shutil.rmtree(group, ignore_errors=True)
                removed = not os.path.isdir(group)
            if removed:
                self._library().forget(print_id)
                self._logger.info(
                    "retention: cleaned rendered group %s (older than %d d)",
                    print_id,
                    days,
                )
        for entry in _listdir_safe(paths.trash_dir):
            path = os.path.join(paths.trash_dir, entry)
            if not is_contained(path, paths.trash_dir):
                continue
            try:
                if os.path.getmtime(path) < cutoff:
                    shutil.rmtree(path, ignore_errors=True)
                    self._logger.info("retention: purged trash/%s", entry)
            except OSError:
                continue

    # ------------------------------------------------------------------
    # Render queue callbacks
    # ------------------------------------------------------------------
    def _render_gate_open(self) -> bool:
        """True when the printer is idle and idle-only rendering is honored."""
        if not self._settings.get_boolean(["render_only_when_idle"]):
            return True
        return not self._print_active()

    def _run_render_job(self, job, cancel, progress_cb) -> str:
        worker = RenderWorker(
            self._logger,
            self._render_paths(),
            timelapse_folder=self._settings.global_get_basefolder("timelapse"),
            fire_movie_done=self._fire_movie_done,
            output_name=self._render_output_name,
            options=RenderOptions(
                ffmpeg_path=self._ffmpeg_path(),
                threads=self._ffmpeg_threads(),
                timeout=(
                    self._settings.get_int(["render_timeout"])
                    or TRANSCODE_TIMEOUT
                ),
                gcode_thumb=self._raw_gcode_thumb(job.get("print_id")),
            ),
        )
        return worker.run(job, cancel, progress_cb)

    def _raw_gcode_thumb(self, print_id) -> Optional[str]:
        """gcode preview PNG for a group, or ``None`` (opt-in / not found).

        Only when ``raw_thumb_from_gcode`` is on. A group knows its print-id
        stem (a sanitized gcode stem), so the connector thumbs dir is matched
        by normalized stem. Returns None on any miss so the render falls back
        to a video-frame thumbnail.
        """
        if not self._settings.get_boolean(["raw_thumb_from_gcode"]):
            return None
        if not print_id or "__" not in print_id:
            return None
        stem = print_id.split("__", 1)[1]
        return gcode_thumb_source_by_stem(self.get_plugin_data_folder(), stem)

    def _render_output_name(self, print_id: str, preset: str) -> str:
        """Output name ``<stem>__<date><suffix>_<preset>_RAW.mp4``.

        Reorders the print-id (``<date>__<stem>``) to job-first so raw renders
        sort next to the job-prefixed SD downloads in the Timelapse tab.
        ``suffix`` is the same optional ``download_suffix`` used for SD
        copies; the trailing ``_RAW`` marks a clip rendered from the raw
        ``/ipcam`` chunks.
        """
        when, sep, stem = print_id.partition("__")
        if not sep:
            stem, when = print_id, ""
        name = stem
        if when:
            name += f"__{when}"
        name += self._sanitized_suffix()
        if preset:
            name += f"_{preset}"
        return f"{name}_RAW.mp4"

    def _on_group_rendered(self, print_id: str) -> None:
        """Re-scan the group so it flips to ``rendered`` (marker written)."""
        self._library().refresh(print_id)

    def _notify_render_job(self, job, state, **extra) -> None:
        if job is None:
            return
        msg = {
            "type": "render_job",
            "jobid": job.get("jobid"),
            "print_id": job.get("print_id"),
            "state": state,
            "percent": job.get("percent", 0),
        }
        msg.update(extra)
        self._plugin_manager.send_plugin_message(self._identifier, msg)

    # ------------------------------------------------------------------
    # API command handlers
    # ------------------------------------------------------------------
    def handle_list_raw_footage(self) -> flask.Response:
        """Return the cached library groups plus the render-queue jobs."""
        return flask.jsonify(
            ok=True,
            groups=self._library().groups(),
            jobs=self._queue().jobs(),
        )

    def handle_scan_raw(self) -> flask.Response:
        """Rescan ``raw/chunks/`` (+ ffprobe) and return the fresh list."""
        groups = self._library().scan()
        threading.Thread(
            target=self._ensure_thumbs, args=(groups,), daemon=True
        ).start()
        return flask.jsonify(ok=True, groups=groups, jobs=self._queue().jobs())

    def handle_start_render(self, data) -> flask.Response:
        """Validate and queue a Concat+Render job for one group."""
        if self._print_active():
            return flask.jsonify(ok=False, reason="printing")
        print_id = data.get("print_id")
        if not is_valid_print_id(print_id):
            return flask.jsonify(ok=False, reason="bad_id")
        group = self._library().get(print_id)
        if group is None:
            return flask.jsonify(ok=False, reason="unknown_group")
        preset = data.get("preset") or render_presets.DEFAULT_PRESET
        chunks = self._select_chunks(group, data.get("chunks"))
        if not chunks:
            return flask.jsonify(ok=False, reason="no_chunks")
        result = self._queue().enqueue(print_id, preset, chunks)
        return flask.jsonify(**result)

    def _select_chunks(self, group, requested) -> list:
        """Resolve the chunk selection against the group's known chunks.

        Defaults to every ``included`` present chunk; a requested subset is
        intersected with the group's chunk names so a tampered payload cannot
        smuggle in foreign file names.
        """
        present = [c["file"] for c in group["chunks"] if c.get("present")]
        if requested is None:
            return [
                c["file"]
                for c in group["chunks"]
                if c.get("present") and c.get("included", True)
            ]
        wanted = {os.path.basename(str(n)) for n in requested}
        return [name for name in present if name in wanted]

    def _auto_render_group(self, print_id: str) -> None:
        """Queue a render for a freshly downloaded group (auto-render).

        Harvest hook for ``auto_render_new_groups``: after a complete chunk
        download the group is refreshed and, if it probed to
        ``chunks_ready``, queued with the configured default preset and
        every included chunk. The queue's idle gate defers the actual
        render while the printer is busy. Best-effort — a failure must
        never break the harvest, so problems are logged, not raised.
        """
        try:
            if not self._settings.get_boolean(["auto_render_new_groups"]):
                return
            if not self._settings.get_boolean(["render_enabled"]):
                return
            group = self._library().refresh(print_id)
            if group is None or group.get("state") != STATE_CHUNKS_READY:
                return
            preset = self._settings.get(["default_preset"]) or ""
            if preset not in render_presets.preset_names():
                preset = render_presets.DEFAULT_PRESET
            chunks = self._select_chunks(group, None)
            if not chunks:
                return
            result = self._queue().enqueue(print_id, preset, chunks)
            if result.get("ok"):
                self._logger.info(
                    "auto-render: queued %s (%s)", print_id, preset
                )
            else:
                self._logger.info(
                    "auto-render: not queued for %s (%s)",
                    print_id,
                    result.get("reason"),
                )
        # harvest must survive this hook
        except Exception:  # noqa: BLE001  # pylint: disable=broad-except
            self._logger.exception("auto-render failed for %s", print_id)

    def handle_cancel_render(self, data) -> flask.Response:
        """Cancel a render job by id."""
        jobid = data.get("jobid")
        if not jobid:
            return flask.jsonify(ok=False, reason="bad_jobid")
        return flask.jsonify(**self._queue().cancel(jobid))

    def handle_delete_group(self, data) -> flask.Response:
        """Permanently delete a group's chunks from disk (the "Discard" button).

        Unlike the retention sweep (which honors ``move_to_trash``), an explicit
        user Discard removes the group directory outright — the user asked for
        the footage to be gone, so it is deleted immediately, not parked in
        ``trash/``.
        """
        if self._print_active():
            return flask.jsonify(ok=False, reason="printing")
        print_id = data.get("print_id")
        if not is_valid_print_id(print_id):
            return flask.jsonify(ok=False, reason="bad_id")
        if self._queue().jobs_active_for(print_id):
            return flask.jsonify(ok=False, reason="rendering")
        if self._delete_group(print_id):
            self._library().forget(print_id)
            return flask.jsonify(ok=True)
        return flask.jsonify(ok=False, reason="unknown_group")

    def _delete_group(self, print_id) -> bool:
        """Permanently remove a group directory from disk (used by Discard)."""
        paths = self._render_paths()
        group = paths.group_dir(print_id)
        # re-assert containment at the sink (group is already vetted by
        # group_dir; this guards against future callers and makes the
        # sanitization visible to taint analysis)
        if (
            group is None
            or not is_contained(group, paths.raw_chunks_dir)
            or not os.path.isdir(group)
        ):
            return False
        try:
            shutil.rmtree(group)
            return True
        except OSError:
            self._logger.warning("could not delete group %s", print_id)
            return False

    def _trash_group(self, print_id) -> bool:
        """Soft-delete a group to ``trash/`` (used by the retention sweep)."""
        paths = self._render_paths()
        group = paths.group_dir(print_id)
        if (
            group is None
            or not is_contained(group, paths.raw_chunks_dir)
            or not os.path.isdir(group)
        ):
            return False
        dest = os.path.join(paths.trash_dir, print_id)
        if not is_contained(dest, paths.trash_dir):
            return False
        try:
            if os.path.exists(dest):
                shutil.rmtree(dest, ignore_errors=True)
            shutil.move(group, dest)
            return True
        except OSError:
            self._logger.warning("could not trash group %s", print_id)
            return False

    def handle_delete_chunks(self, data) -> flask.Response:
        """Permanently delete selected chunks (with their slot-pair siblings).

        Chunks that share a recording (same ``<dt>`` in
        ``ipcam-record.<dt>.<slot>.avi``) belong together, so any requested
        chunk pulls in its pair siblings. Deletion is immediate and
        irreversible — no trash. ``order.json`` is rewritten so the group's
        remaining chunks stay consistent, and an emptied group is forgotten.
        """
        if self._print_active():
            return flask.jsonify(ok=False, reason="printing")
        print_id = data.get("print_id")
        if not is_valid_print_id(print_id):
            return flask.jsonify(ok=False, reason="bad_id")
        if self._queue().jobs_active_for(print_id):
            return flask.jsonify(ok=False, reason="rendering")
        group = self._library().get(print_id)
        if group is None:
            return flask.jsonify(ok=False, reason="unknown_group")
        targets = self._expand_pairs(group, data.get("chunks"))
        if not targets:
            return flask.jsonify(ok=False, reason="no_chunks")
        removed = self._remove_chunk_files(print_id, targets)
        self._rewrite_order(print_id, removed)
        self._library().refresh(print_id)
        if self._library().get(print_id) is None:
            self._library().forget(print_id)
        return flask.jsonify(ok=True, removed=sorted(removed))

    def _expand_pairs(self, group, requested) -> set:
        """Grow a requested chunk selection to whole slot-pairs.

        Maps each requested name to its recording key and returns every present
        chunk in the group sharing that key, so slot siblings stay together.
        A tampered payload can only ever select the group's own chunk names.
        """
        present = {
            c["file"] for c in group.get("chunks", []) if c.get("present")
        }
        wanted = {os.path.basename(str(n)) for n in (requested or [])}
        wanted &= present
        keys = {_pair_key(name) for name in wanted}
        return {name for name in present if _pair_key(name) in keys}

    def _remove_chunk_files(self, print_id, names) -> set:
        """``os.remove`` each contained chunk; return names actually gone."""
        paths = self._render_paths()
        group = paths.group_dir(print_id)
        if group is None:
            return set()
        removed = set()
        for name in names:
            path = paths.chunk_path(print_id, name)
            # re-assert containment at the sink (path is already vetted by
            # chunk_path; this guards against future callers and makes the
            # sanitization visible to taint analysis)
            if path is None or not is_contained(path, group):
                continue
            try:
                os.remove(path)
                removed.add(name)
            except FileNotFoundError:
                removed.add(name)
            except OSError:
                self._logger.warning(
                    "could not delete chunk %s/%s", print_id, name
                )
        return removed

    def _rewrite_order(self, print_id, removed) -> None:
        """Drop ``removed`` from ``order.json`` (atomic; best-effort)."""
        if not removed:
            return
        paths = self._render_paths()
        group = paths.group_dir(print_id)
        order_file = paths.order_file(print_id)
        # re-assert containment at the sink (order_file is already vetted by
        # order_file/group_dir; this guards against future callers and makes
        # the sanitization visible to taint analysis)
        if (
            group is None
            or order_file is None
            or not is_contained(order_file, group)
            or not os.path.isfile(order_file)
        ):
            return
        try:
            with open(order_file, encoding="utf-8") as fh:
                order = json.load(fh)
        except (OSError, ValueError):
            return
        if not isinstance(order, list):
            return
        kept = [
            item
            for item in order
            if not (
                isinstance(item, dict)
                and os.path.basename(str(item.get("file", ""))) in removed
            )
        ]
        tmp = order_file + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(kept, fh)
            os.replace(tmp, order_file)
        except OSError:
            self._logger.warning(
                "could not rewrite order.json for %s", print_id
            )

    def handle_render_status(self) -> flask.Response:
        """Return the render-queue jobs (polling fallback for the UI)."""
        return flask.jsonify(ok=True, jobs=self._queue().jobs())

    def handle_ffprobe_status(self) -> flask.Response:
        """Report ffprobe availability (mirrors ``ffmpeg_status``)."""
        return flask.jsonify(ok=True, ffprobe=self._ffprobe_runner().status())

    def handle_render_ffmpeg_status(self) -> flask.Response:
        """Report the render pipeline's ffmpeg availability.

        Unlike ``ffmpeg_status`` (the transcoder's ffmpeg for the Timelapse
        tab), this resolves the Render-tab ``ffmpeg_path`` override with
        OctoPrint's ``webcam.ffmpeg`` fallback — the binary render jobs
        actually run.
        """
        path = self._ffmpeg_path()
        executable = bool(
            path and os.path.isfile(path) and os.access(path, os.X_OK)
        )
        return flask.jsonify(
            ok=True,
            ffmpeg={
                "path": path,
                "configured": bool(path),
                "executable": executable,
            },
        )

    # ------------------------------------------------------------------
    # Raw preview thumbnails
    # ------------------------------------------------------------------
    def _ensure_thumbs(self, groups) -> None:
        """Generate any missing raw-preview thumbnails for the groups."""
        for group in groups:
            if group.get("has_thumb"):
                continue
            self._make_raw_thumb(group)

    def _make_raw_thumb(self, group) -> None:
        """Grab the last frame of the group's last present chunk (plan §13).

        ``ffmpeg -sseof -1 -i <last>.avi -frames:v 1 <out>.jpg`` shows the
        finished part for best recognition. Best-effort; a failure is logged.
        """
        print_id = group["print_id"]
        if not is_valid_print_id(print_id):
            return
        present = [c for c in group["chunks"] if c.get("present")]
        if not present:
            return
        last = present[-1]["file"]
        paths = self._render_paths()
        src = paths.chunk_path(print_id, last)
        out = paths.thumb_file(print_id)
        ffmpeg = self._ffmpeg_path()
        if not src or not out or not ffmpeg:
            return
        # re-assert containment at the sink (src/out are already vetted by
        # chunk_path/thumb_file; this guards against future callers and
        # makes the sanitization visible to taint analysis)
        if not is_contained(src, paths.raw_chunks_dir) or not is_contained(
            out, paths.thumbs_dir
        ):
            return
        tmp = out + ".part"
        # ffmpeg picks the output muxer from the file extension, which the
        # ".part" temp name breaks — force the single-image muxer instead.
        cmd = [
            ffmpeg,
            "-y",
            "-sseof",
            "-1",
            "-i",
            src,
            "-frames:v",
            "1",
            "-f",
            "image2",
            tmp,
        ]
        if self._run_thumb_cmd(cmd) and os.path.isfile(tmp):
            try:
                os.replace(tmp, out)
            except OSError:
                _silent_remove(tmp)
        else:
            _silent_remove(tmp)
            self._logger.info("raw thumb failed for %s", print_id)

    def _run_thumb_cmd(self, cmd) -> bool:
        try:
            proc = subprocess.run(  # nosec B603 - no shell, fixed argv
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=THUMB_TIMEOUT,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return proc.returncode == 0

    def handle_raw_thumb(self, print_id) -> flask.Response:
        """Serve a group's raw-preview JPEG, or 404."""
        if not is_valid_print_id(print_id):
            flask.abort(404)
        paths = self._render_paths()
        out = paths.thumb_file(print_id)
        # re-assert containment at the sink (out is already vetted by
        # thumb_file; this guards against future callers and makes the
        # sanitization visible to taint analysis)
        if (
            out is None
            or not is_contained(out, paths.thumbs_dir)
            or not os.path.isfile(out)
        ):
            flask.abort(404)
        with open(out, "rb") as fh:
            data = fh.read()
        resp = flask.Response(data, mimetype="image/jpeg")
        resp.headers["Cache-Control"] = "private, max-age=3600"
        return resp


def _silent_remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _listdir_safe(path: str) -> list:
    try:
        return os.listdir(path)
    except OSError:
        return []


def _pair_key(name: str) -> str:
    """Recording key grouping a chunk with its slot-pair siblings.

    ``/ipcam`` chunks are named ``ipcam-record.<dt>.<slot>.avi``; the ``<dt>``
    part is shared by every slot of one recording, so it keys a pair. Names not
    matching the pattern fall back to the whole name (each is its own group).
    """
    match = _PAIR_KEY_RE.match(name or "")
    return match.group(1) if match else (name or "")


_PAIR_KEY_RE = re.compile(r"^(.*)\.\d+\.avi$", re.IGNORECASE)
