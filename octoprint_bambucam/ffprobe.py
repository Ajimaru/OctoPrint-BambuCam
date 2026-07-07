"""Probe a video file's metadata via ``ffprobe`` (JSON output).

The render pipeline needs duration, resolution, frame rate, codec and size of
the harvested ``/ipcam`` chunks to drive the library table and the render
defaults. ``FfprobeRunner`` wraps a single ``ffprobe -print_format json`` call
behind an injectable runner so it stays unit-testable without ffprobe present.

The ffprobe binary path mirrors the transcoder rule (plan §Opt. 16): the caller
passes an explicit plugin setting, falling back to the binary next to
OctoPrint's ``webcam.ffmpeg`` when that setting is empty.
"""

import json
import os
import subprocess  # nosec B404 - fixed argv list, no shell; path from settings
from typing import Optional

PROBE_TIMEOUT = 30


class FfprobeError(Exception):
    """ffprobe failed; carries a short ``reason`` for logging/UI."""

    def __init__(self, reason: str, message: str = ""):
        super().__init__(message or reason)
        self.reason = reason


def fallback_ffprobe_path(ffmpeg_path: Optional[str]) -> Optional[str]:
    """Guess the ffprobe path sitting next to ``ffmpeg_path``.

    OctoPrint only stores the ffmpeg binary; ffprobe normally lives in the
    same directory under an analogous name. Returns ``None`` when ffmpeg is
    unset or no sibling ffprobe exists.
    """
    base = (ffmpeg_path or "").strip()
    if not base:
        return None
    directory, name = os.path.split(base)
    guess_name = name.replace("ffmpeg", "ffprobe")
    if guess_name == name:
        guess_name = "ffprobe"
    candidate = os.path.join(directory, guess_name) if directory else guess_name
    if os.path.isfile(candidate):
        return candidate
    return None


class FfprobeRunner:
    """Probe video metadata with ``ffprobe``, JSON output.

    Construct with the resolved ffprobe path (empty/``None`` means ffprobe is
    not configured and :meth:`available` is ``False``). ``runner`` is the
    injection seam used by tests to avoid spawning a real process.
    """

    def __init__(
        self,
        logger,
        *,
        ffprobe_path: Optional[str],
        runner=None,
    ):
        self._logger = logger
        self._ffprobe = (ffprobe_path or "").strip() or None
        self._runner = runner or _run_ffprobe

    def available(self) -> bool:
        """True when an ffprobe path is configured."""
        return self._ffprobe is not None

    def status(self) -> dict:
        """Report ffprobe availability for the settings indicator.

        ``configured`` is True when a path is set; ``executable`` additionally
        checks the file exists and is runnable (mirrors the transcoder status
        contract so the UI can reuse the same indicator).
        """
        path = self._ffprobe or ""
        executable = bool(
            path and os.path.isfile(path) and os.access(path, os.X_OK)
        )
        return {
            "path": path,
            "configured": self._ffprobe is not None,
            "executable": executable,
        }

    def probe(self, video_path: str) -> dict:
        """Return ``{duration, width, height, fps, codec, size}`` for a video.

        Runs ``ffprobe -show_format -show_streams`` and reduces the JSON to the
        fields the library needs. ``size`` is taken from the local file (not
        the probe) so it is always present. Raises :class:`FfprobeError` when
        ffprobe is unconfigured, the file is missing, or ffprobe fails.
        """
        if not self.available():
            raise FfprobeError("no_ffprobe", "ffprobe path not configured")
        if not os.path.isfile(video_path):
            raise FfprobeError("not_found", video_path)
        cmd = [
            self._ffprobe,
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            video_path,
        ]
        rc, out = self._runner(cmd, PROBE_TIMEOUT)
        if rc != 0:
            raise FfprobeError("ffprobe_failed", f"exit {rc}")
        try:
            data = json.loads(out or "{}")
        except (ValueError, TypeError) as exc:
            raise FfprobeError("bad_json", str(exc)) from exc
        meta = _reduce_probe(data)
        meta["size"] = self._file_size(video_path)
        return meta

    @staticmethod
    def _file_size(path: str) -> Optional[int]:
        try:
            return os.path.getsize(path)
        except OSError:
            return None


def _reduce_probe(data: dict) -> dict:
    """Reduce raw ffprobe JSON to the library's metadata fields."""
    fmt = data.get("format", {}) or {}
    streams = data.get("streams", []) or []
    video = next(
        (s for s in streams if s.get("codec_type") == "video"),
        {},
    )
    duration = _to_float(fmt.get("duration") or video.get("duration"))
    return {
        "duration": duration,
        "width": _to_int(video.get("width")),
        "height": _to_int(video.get("height")),
        "fps": _parse_fps(video.get("avg_frame_rate")),
        "codec": video.get("codec_name"),
    }


def _parse_fps(rate: Optional[str]) -> Optional[float]:
    """Parse an ffprobe ``"num/den"`` frame-rate string into a float."""
    if not rate or "/" not in rate:
        return _to_float(rate)
    num, den = rate.split("/", 1)
    num_f = _to_float(num)
    den_f = _to_float(den)
    if num_f is None or not den_f:
        return None
    return round(num_f / den_f, 3)


def _to_float(value) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _run_ffprobe(cmd: list, timeout: int):
    """Run ffprobe without a shell, capturing stdout JSON.

    Returns ``(returncode, stdout)``. A spawn failure or timeout surfaces as a
    non-zero return code with empty output so the caller raises a clean
    :class:`FfprobeError`.
    """
    try:
        proc = subprocess.run(  # nosec B603 - no shell, fixed argv list
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
            text=True,
        )
    except (OSError, subprocess.SubprocessError):
        return 1, ""
    return proc.returncode, proc.stdout
