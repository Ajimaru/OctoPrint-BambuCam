"""FFmpeg concat + render worker producing the final timelapse ``.mp4``.

A render job runs in two ffmpeg steps (plan §3.4): a lossless concat of the
selected chunks (``-c copy`` — every MJPEG frame is a keyframe) followed by an
H.264 re-encode with the chosen preset's scale/speed/crf. The output is written
to a ``work/`` temp and atomically moved into OctoPrint's timelapse folder, then
``MovieDone`` is fired so the native tab refreshes. On success the raw chunk
group is kept and stamped with a ``rendered.json`` marker so the Raw Files tab
can show it as ``rendered`` (and offer a re-render/discard) instead of the
group silently vanishing.

The worker reuses the ffmpeg runner from :mod:`transcode` for live progress and
the no-shell argv discipline. ``RenderWorker.run`` is the callable the
:class:`render_queue.RenderQueue` invokes.
"""

import json
import logging
import os
import shutil
import time
from dataclasses import dataclass
from typing import Callable, Optional

from . import render_paths, render_presets
from .gcode_thumb import write_gcode_thumbnail
from .transcode import TRANSCODE_TIMEOUT, TranscodeError, _run_command


class RenderError(Exception):
    """A render step failed; carries a short ``reason``."""

    def __init__(self, reason: str, message: str = ""):
        super().__init__(message or reason)
        self.reason = reason


@dataclass
class RenderOptions:
    """ffmpeg encoding knobs for a :class:`RenderWorker`.

    Groups the tuning/injection parameters (kept out of the constructor's
    positional deps) so the worker stays under the argument-count limit.
    ``runner`` is the injection seam for tests so no real ffmpeg is spawned;
    ``gcode_thumb`` is an optional preview PNG to use as the clip thumbnail
    instead of a video frame.
    """

    ffmpeg_path: Optional[str]
    runner: Optional[Callable] = None
    timeout: int = TRANSCODE_TIMEOUT
    threads: int = 1
    gcode_thumb: Optional[str] = None


class RenderWorker:
    """Concat + re-encode a chunk group into OctoPrint's timelapse folder.

    Construct with the resolved ffmpeg path, the :class:`RenderPaths`, the
    timelapse output folder, and callbacks the plugin supplies
    (``fire_movie_done``, ``output_name``). ``runner`` is the injection seam
    for tests so no real ffmpeg is spawned.
    """

    def __init__(
        self,
        logger: logging.Logger,
        paths,
        *,
        timelapse_folder: str,
        fire_movie_done: Callable[[str], None],
        output_name: Callable[[str, str], str],
        options: RenderOptions,
    ):
        self._logger = logger
        self._paths = paths
        self._ffmpeg = (options.ffmpeg_path or "").strip() or None
        self._timelapse_folder = timelapse_folder
        self._fire_movie_done = fire_movie_done
        self._output_name = output_name
        self._runner = options.runner or _run_command
        self._timeout = options.timeout
        # Optional gcode preview PNG (Bambu Connector) to use as the clip's
        # thumbnail instead of a video frame; None → use the last video frame.
        self._gcode_thumb = options.gcode_thumb
        # ffmpeg -threads for the encode step; 0 lets ffmpeg pick (all cores),
        # a positive value caps CPU use (default 1, Pi-friendly). Never < 0.
        self._threads = max(int(options.threads), 0)

    def available(self) -> bool:
        """True when an ffmpeg path is configured."""
        return self._ffmpeg is not None

    def run(self, job: dict, cancel, progress_cb: Callable) -> str:
        """Execute a render job; return the published ``.mp4`` path.

        ``job`` carries ``print_id``, ``preset`` and ``chunks`` (the selected
        chunk file names). ``cancel`` is checked between/within ffmpeg steps;
        ``progress_cb(phase, percent)`` reports ``concat``/``render`` progress.
        Raises :class:`RenderError` on any failure. Cleans up its ``work/``
        temps in all paths.
        """
        if not self.available():
            raise RenderError("no_ffmpeg", "ffmpeg path not configured")
        print_id = job["print_id"]
        sources = self._resolve_sources(print_id, job.get("chunks") or [])
        concat_path = os.path.join(
            self._paths.work_dir, f"{job['jobid']}.concat.avi"
        )
        list_path = os.path.join(
            self._paths.work_dir, f"{job['jobid']}.list.txt"
        )
        tmp_mp4 = os.path.join(self._paths.work_dir, f"{job['jobid']}.tmp.mp4")
        try:
            concat_src = self._concat(
                sources, list_path, concat_path, cancel, progress_cb
            )
            self._render(
                concat_src, tmp_mp4, job.get("preset"), cancel, progress_cb
            )
            final = self._publish(print_id, job.get("preset"), tmp_mp4)
        finally:
            _silent_remove(list_path)
            _silent_remove(concat_path)
            _silent_remove(tmp_mp4)
        self._write_thumbnail(final)
        self._fire_movie_done(final)
        self._mark_rendered(print_id, final, job.get("preset"))
        return final

    def _resolve_sources(self, print_id: str, chunks: list) -> list:
        """Map selected chunk names to contained absolute paths.

        Rejects any name that does not resolve inside the group directory, so a
        tampered API payload can never feed ffmpeg an outside file.
        """
        resolved = []
        for name in chunks:
            path = self._paths.chunk_path(print_id, name)
            if path is None or not os.path.isfile(path):
                raise RenderError("bad_chunk", str(name))
            resolved.append(path)
        if not resolved:
            raise RenderError("no_chunks", print_id)
        return resolved

    def _concat(
        self, sources, list_path, concat_path, cancel, progress_cb
    ) -> str:
        """Lossless ``-c copy`` concat; return the path to feed the render.

        A single-chunk selection skips ffmpeg entirely and renders the lone
        chunk directly (plan §3.4).
        """
        if len(sources) == 1:
            return sources[0]
        self._write_concat_list(list_path, sources)
        progress_cb("concat", 0)
        cmd = [
            self._ffmpeg,
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            list_path,
            "-c",
            "copy",
            "-y",
            concat_path,
        ]
        self._exec(cmd, cancel)
        if not os.path.isfile(concat_path):
            raise RenderError("concat_failed", "no concat output")
        progress_cb("concat", 100)
        return concat_path

    @staticmethod
    def _write_concat_list(list_path: str, sources: list) -> None:
        """Write the ffmpeg concat demuxer list (single-quoted, escaped)."""
        with open(list_path, "w", encoding="utf-8") as fh:
            for src in sources:
                safe = src.replace("'", "'\\''")
                fh.write(f"file '{safe}'\n")

    def _render(self, src, tmp_mp4, preset_name, cancel, progress_cb) -> None:
        preset = render_presets.get_preset(preset_name)
        vf = render_presets.build_vf(preset)
        cmd = [
            self._ffmpeg,
            "-y",
            "-i",
            src,
            "-vf",
            vf,
            "-c:v",
            "libx264",
            "-preset",
            preset["x264_preset"],
            "-crf",
            str(preset["crf"]),
            "-threads",
            str(self._threads),
            "-pix_fmt",
            "yuv420p",
            "-an",
            tmp_mp4,
        ]

        def _on_pct(pct: int) -> None:
            progress_cb("render", pct)

        self._exec(cmd, cancel, progress_cb=_on_pct)
        if not os.path.isfile(tmp_mp4) or os.path.getsize(tmp_mp4) == 0:
            raise RenderError("empty_output", "ffmpeg produced no data")

    def _publish(self, print_id, preset_name, tmp_mp4) -> str:
        """Atomically move the temp ``.mp4`` into OctoPrint's timelapse folder.

        Builds a collision-free name from the plugin's pattern, re-asserts
        containment, then ``os.replace`` (atomic same-device move within the
        plugin? no — work dir and timelapse may differ devices, so copy+replace
        when needed).
        """
        name = self._output_name(print_id, preset_name or "")
        stem, ext = os.path.splitext(name)
        if not ext:
            ext = ".mp4"
        safe = render_paths.collision_safe(self._timelapse_folder, stem, ext)
        if safe is None:
            raise RenderError("name_conflict", print_id)
        dest = os.path.join(self._timelapse_folder, safe)
        if not render_paths.is_contained(dest, self._timelapse_folder):
            raise RenderError("bad_dest", dest)
        self._atomic_move(tmp_mp4, dest)
        self._logger.info("rendered %s -> %s", print_id, dest)
        return dest

    @staticmethod
    def _atomic_move(src: str, dest: str) -> None:
        """Move ``src`` to ``dest`` atomically, spanning devices if needed.

        Same device → ``os.replace``. Cross device → copy to a sibling temp of
        ``dest`` then ``os.replace`` so the final name appears atomically.
        """
        try:
            os.replace(src, dest)
            return
        except OSError:
            pass
        tmp_dest = dest + ".part"
        shutil.copyfile(src, tmp_dest)
        os.replace(tmp_dest, dest)

    def _mark_rendered(self, print_id: str, output: str, preset) -> None:
        """Stamp the group with a ``rendered.json`` marker (atomic write).

        The chunks stay on disk so the Raw Files tab keeps listing the group
        as ``rendered`` and a re-render with another preset stays possible.
        Best-effort: a marker failure must not undo a successful render.
        """
        marker = self._paths.rendered_marker(print_id)
        if marker is None or not os.path.isdir(os.path.dirname(marker)):
            return
        payload = {
            "rendered_at": time.strftime("%Y-%m-%d %H:%M"),
            "output": os.path.basename(output),
            "preset": preset,
        }
        tmp = marker + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
            os.replace(tmp, marker)
        except OSError:
            self._logger.warning(
                "could not write rendered marker for %s", print_id
            )

    def _exec(self, cmd, cancel, *, progress_cb=None) -> None:
        self._logger.debug("ffmpeg render cmd: %s", " ".join(cmd))
        rc, err = self._runner(cmd, self._timeout, progress_cb, cancel)
        if cancel.is_set():
            raise RenderError("cancelled", "job cancelled")
        if rc != 0:
            tail = (err or "").strip().splitlines()[-1:] or [""]
            raise RenderError("ffmpeg_failed", tail[0])

    def _write_thumbnail(self, mp4_path: str) -> None:
        """Write a ``<mp4>.thumb.jpg`` (best-effort, never raises).

        OctoPrint's native Timelapse list shows ``<movie>.thumb.jpg`` next to
        each clip; without one it falls back to a blank tile. When a gcode
        preview PNG was supplied (the opt-in "use gcode preview" setting) it is
        used as the thumbnail; otherwise the video's last frame (the finished
        print) is grabbed with ``-sseof -1``, which makes a better thumbnail
        than the empty-bed first frame.
        """
        ffmpeg = self._ffmpeg
        if ffmpeg is None:  # unreachable via run() (guarded by available())
            return
        thumb = mp4_path + ".thumb.jpg"
        if self._gcode_thumb and write_gcode_thumbnail(
            ffmpeg, self._gcode_thumb, thumb, self._runner, self._timeout
        ):
            return
        cmd = [
            ffmpeg,
            "-sseof",
            "-1",
            "-i",
            mp4_path,
            "-frames:v",
            "1",
            "-q:v",
            "3",
            "-y",
            thumb,
        ]
        try:
            self._runner(cmd, self._timeout, None, None)
        except (RenderError, TranscodeError, OSError, ValueError):
            # thumbnail is cosmetic
            self._logger.warning("thumbnail generation failed for %s", mp4_path)


def _silent_remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass
