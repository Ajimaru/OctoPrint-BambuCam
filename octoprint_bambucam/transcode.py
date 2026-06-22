"""Transcode a Bambu timelapse ``.avi`` (Motion-JPEG) into a playable ``.mp4``.

The MJPEG/AVI the printer records does not play in most browsers, so after a
copy/move we re-encode to H.264 ``.mp4`` and drop the ``.avi`` (the SD-card
original is untouched). Reuses OctoPrint's own webcam ffmpeg config.
"""

import os
import re
import shlex
import subprocess  # nosec B404 - fixed argv list, no shell; path from OctoPrint
import time
from typing import Callable, Optional

TRANSCODE_TIMEOUT = 1800  # 30 min — large timelapses re-encode slowly on a Pi

_DURATION_RE = re.compile(r"Duration: (\d{2}):(\d{2}):(\d{2})\.(\d{2})")
_TIME_RE = re.compile(r"time=(\d{2}):(\d{2}):(\d{2})\.(\d{2})")


def _hms_to_seconds(h, m, s, cs) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(cs) / 100.0


class TranscodeError(Exception):
    """ffmpeg transcoding failed; carries a short ``reason`` for the UI."""

    def __init__(self, reason: str, message: str = ""):
        super().__init__(message or reason)
        self.reason = reason


class TimelapseTranscoder:
    """Re-encode an ``.avi`` to ``.mp4`` using OctoPrint's ffmpeg settings.

    Construct with the resolved ffmpeg parameters (so the plugin pulls them
    from ``self._settings`` and this class stays unit-testable with a fake
    runner). ``ffmpeg_path`` of ``None``/empty means ffmpeg is not configured
    and :meth:`available` is ``False``.
    """

    def __init__(
        self,
        logger,
        *,
        ffmpeg_path: Optional[str],
        videocodec: str = "libx264",
        bitrate: str = "10000k",
        threads: int = 1,
        thumbnail_commandline: Optional[str] = None,
        runner=None,
    ):
        self._logger = logger
        self._ffmpeg = (ffmpeg_path or "").strip() or None
        self._videocodec = videocodec or "libx264"
        self._bitrate = bitrate or "10000k"
        self._threads = threads or 1
        self._thumb_cmd = thumbnail_commandline
        self._runner = runner or _run_command  # injection seam for tests

    def available(self) -> bool:
        """True when ffmpeg is configured (path set in OctoPrint webcam)."""
        return self._ffmpeg is not None

    def status(self) -> dict:
        """Report ffmpeg availability for the settings indicator.

        ``configured`` is True when OctoPrint has an ffmpeg path set;
        ``executable`` additionally checks that the file exists and is runnable
        (a stale/typo'd path is configured-but-not-executable). ``path`` is the
        configured path (or ``""``) so the UI can show it.
        """
        path = self._ffmpeg or ""
        executable = bool(
            path and os.path.isfile(path) and os.access(path, os.X_OK)
        )
        return {
            "path": path,
            "configured": self._ffmpeg is not None,
            "executable": executable,
        }

    def transcode(
        self,
        avi_path: str,
        mp4_path: str,
        *,
        progress_cb: Optional[Callable[[int], None]] = None,
    ) -> None:
        """Re-encode ``avi_path`` → ``mp4_path`` (H.264), atomically.

        Writes to a temp ``.part`` next to ``mp4_path`` then renames, so a
        failed/interrupted run never leaves a half ``.mp4`` behind. Raises
        :class:`TranscodeError` on any failure (caller keeps the ``.avi`` and
        reports a warning). ``progress_cb(percent)`` is invoked as ffmpeg
        reports progress (0-100, whole percents).
        """
        if not self.available():
            raise TranscodeError("no_ffmpeg", "ffmpeg path not configured")

        tmp_out = mp4_path + ".part"
        cmd = [
            self._ffmpeg,
            "-i",
            avi_path,
            "-c:v",
            self._videocodec,
            "-pix_fmt",
            "yuv420p",
            "-b:v",
            self._bitrate,
            "-threads",
            str(self._threads),
            "-f",
            "mp4",
            "-y",
            tmp_out,
        ]
        try:
            self._exec(cmd, progress_cb=progress_cb)
            if not os.path.exists(tmp_out) or os.path.getsize(tmp_out) == 0:
                raise TranscodeError("empty_output", "ffmpeg produced no data")
            os.replace(tmp_out, mp4_path)
        except TranscodeError:
            self._cleanup(tmp_out)
            raise
        except OSError as exc:
            self._cleanup(tmp_out)
            raise TranscodeError("io_error", str(exc)) from exc
        self._logger.info("transcoded %s -> %s", avi_path, mp4_path)

    def create_thumbnail(self, mp4_path: str, thumb_path: str) -> bool:
        """Best-effort ``<mp4>.thumb.jpg`` from the last frame.

        Uses OctoPrint's ``ffmpegThumbnailCommandline`` template if available.
        Returns True on success; a failure is non-fatal (returns False) — the
        movie is still playable without a thumbnail.
        """
        if not self.available() or not self._thumb_cmd:
            return False
        try:
            cmd_str = self._thumb_cmd.format(
                ffmpeg=self._ffmpeg, input=mp4_path, output=thumb_path
            )
            self._exec(_split_commandline(cmd_str))
            return os.path.exists(thumb_path)
        except (TranscodeError, OSError, ValueError):
            self._logger.warning("thumbnail generation failed for %s", mp4_path)
            return False

    def _exec(self, cmd: list[str], *, progress_cb=None) -> None:
        self._logger.debug("ffmpeg cmd: %s", " ".join(cmd))
        rc, err = self._runner(cmd, TRANSCODE_TIMEOUT, progress_cb)
        if rc != 0:
            tail = (err or "").strip().splitlines()[-1:] or [""]
            raise TranscodeError("ffmpeg_failed", tail[0])

    @staticmethod
    def _cleanup(path: str) -> None:
        try:
            os.remove(path)
        except OSError:
            pass


def _split_commandline(cmd_str: str) -> list[str]:
    """Split an OctoPrint ffmpeg commandline template into argv (no shell)."""
    return shlex.split(cmd_str)


def _run_command(cmd: list[str], timeout: int, progress_cb=None):
    """Run ffmpeg without a shell, streaming stderr for live progress.

    Returns ``(returncode, stderr_tail)``. Parses ffmpeg's ``Duration:`` (once)
    and ``time=`` (repeatedly) lines into a 0-100 percentage and feeds it to
    ``progress_cb`` (throttled to whole-percent changes). Keeps only the last
    stderr lines so a failure message can be surfaced without buffering all of
    ffmpeg's noisy output.
    """
    try:
        proc = subprocess.Popen(  # nosec B603 - no shell, argv list
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            bufsize=1,
            universal_newlines=True,
        )
    except OSError as exc:
        raise TranscodeError("ffmpeg_failed", str(exc)) from exc

    deadline = time.monotonic() + timeout
    duration = 0.0
    last_pct = -1
    tail: list[str] = []
    assert proc.stderr is not None
    for line in _iter_ffmpeg_lines(proc.stderr):
        tail.append(line)
        del tail[:-20]
        if duration == 0.0:
            m = _DURATION_RE.search(line)
            if m:
                duration = _hms_to_seconds(*m.groups())
        if progress_cb is not None and duration > 0:
            m = _TIME_RE.search(line)
            if m:
                cur = _hms_to_seconds(*m.groups())
                pct = max(0, min(100, int(cur / duration * 100)))
                if pct != last_pct:
                    last_pct = pct
                    progress_cb(pct)
        if time.monotonic() > deadline:
            proc.kill()
            proc.wait()
            raise TranscodeError("timeout", "ffmpeg exceeded the time limit")

    proc.wait()
    return proc.returncode, "\n".join(tail)


def _iter_ffmpeg_lines(stream):
    """Yield ffmpeg stderr lines, splitting on both ``\\r`` and ``\\n``.

    Flushes an over-long line as its own "line" so a stream that never emits a
    break (pathological/hung ffmpeg) can't grow the buffer without bound or
    starve the caller's timeout check.
    """
    buf = ""
    while True:
        chunk = stream.read(1)
        if not chunk:
            if buf:
                yield buf
            return
        if chunk in ("\r", "\n"):
            if buf:
                yield buf
                buf = ""
        else:
            buf += chunk
            if (
                len(buf) >= 1024
            ):  # ffmpeg lines are short; flush runaway buffers
                yield buf
                buf = ""
