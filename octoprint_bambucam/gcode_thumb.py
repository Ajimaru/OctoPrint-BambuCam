"""Locate a print job's gcode preview image (from OctoPrint-BambuConnector).

When the user opts in, the rendered clip's thumbnail is the print job's slicer
plate preview instead of a video frame. Bambu Connector stores that preview at
``<basedir>/data/bambu_connector/thumbs/<gcode-name>/plate_1.png``. This module
resolves that path safely (no traversal outside the connector thumbs dir) and
converts it to the ``.thumb.jpg`` OctoPrint's Timelapse list expects.
"""

import os
import re
from typing import Callable, Optional

from .transcode import TranscodeError

# Bambu Connector's data folder (sibling of our own plugin data folder) and the
# per-job preview it writes there.
_CONNECTOR_DIRNAME = "bambu_connector"
_THUMBS_DIRNAME = "thumbs"
_PLATE_FILENAME = "plate_1.png"

# Match the print-id stem normalization: collapse anything outside [\w.-] to _.
_STEM_ILLEGAL_RE = re.compile(r"[^\w.-]")


def _norm_stem(name: str) -> str:
    """Normalize a name to compare a print-id stem with a connector folder.

    Strips the ``.gcode``/``.3mf`` extensions and collapses out-of-grammar
    characters to ``_``, matching how ``render_paths._gcode_stem`` builds the
    print-id stem — so ``A1+Toolbox_TPU.gcode.3mf`` and the print-id stem
    ``A1_Toolbox_TPU`` compare equal.
    """
    base = os.path.basename(name or "")
    # Drop a trailing .3mf then a trailing .gcode (the Bambu double extension).
    for ext in (".3mf", ".gcode"):
        if base.lower().endswith(ext):
            base = base[: -len(ext)]
    return _STEM_ILLEGAL_RE.sub("_", base)


def _connector_thumbs_dir(plugin_data_folder: str) -> str:
    data_root = os.path.dirname(os.path.abspath(plugin_data_folder))
    return os.path.join(data_root, _CONNECTOR_DIRNAME, _THUMBS_DIRNAME)


def gcode_thumb_source(
    plugin_data_folder: str, gcode_name: str
) -> Optional[str]:
    """Path to a print job's gcode preview PNG by exact file name, or ``None``.

    ``gcode_name`` is the job's file name (e.g.
    ``OctoPrint-Upload_red.gcode.3mf``); its basename is the per-job
    sub-directory under Bambu Connector's ``thumbs``. Returns the
    ``plate_1.png`` path only when it exists and stays contained inside the
    connector thumbs directory. Used by the SD-timelapse path, which knows the
    real gcode name from the PrintDone payload.
    """
    if not gcode_name:
        return None
    thumbs_dir = _connector_thumbs_dir(plugin_data_folder)
    job = os.path.basename(gcode_name)
    if not job or job in (".", ".."):
        return None
    return _contained_plate(thumbs_dir, job)


def gcode_thumb_source_by_stem(
    plugin_data_folder: str, stem: str
) -> Optional[str]:
    """Path to a gcode preview PNG matched by a print-id **stem**, or ``None``.

    The Raw Files render only knows a group's print-id stem (a sanitized gcode
    stem), not the original file name, so this scans Bambu Connector's thumbs
    directory for the folder whose normalized name equals ``stem`` and returns
    its ``plate_1.png``. Falls back to ``None`` when nothing matches.
    """
    if not stem:
        return None
    thumbs_dir = _connector_thumbs_dir(plugin_data_folder)
    try:
        entries = os.listdir(thumbs_dir)
    except OSError:
        return None
    target = _norm_stem(stem)
    for entry in entries:
        if _norm_stem(entry) == target:
            hit = _contained_plate(thumbs_dir, entry)
            if hit:
                return hit
    return None


def _contained_plate(thumbs_dir: str, job: str) -> Optional[str]:
    """``<thumbs_dir>/<job>/plate_1.png`` if it exists and stays contained."""
    candidate = os.path.join(thumbs_dir, job, _PLATE_FILENAME)
    base = os.path.realpath(thumbs_dir)
    real = os.path.realpath(candidate)
    if real != base and not real.startswith(base + os.sep):
        return None
    return candidate if os.path.isfile(candidate) else None


def write_gcode_thumbnail(
    ffmpeg: str,
    src_png: str,
    thumb_path: str,
    runner: Callable,
    timeout: int,
) -> bool:
    """Convert the gcode preview PNG into ``thumb_path`` (JPEG); return success.

    Uses ffmpeg (already required by the pipeline) to transcode the PNG to the
    ``.thumb.jpg`` OctoPrint expects, scaling it down to a sensible list size.
    Best-effort: any failure returns ``False`` so the caller can fall back to a
    video-frame thumbnail.
    """
    if not ffmpeg or not src_png:
        return False
    cmd = [
        ffmpeg,
        "-i",
        src_png,
        "-vf",
        "scale=640:-1",
        "-frames:v",
        "1",
        "-q:v",
        "3",
        "-y",
        thumb_path,
    ]
    try:
        rc, _err = runner(cmd, timeout, None, None)
    except (TranscodeError, OSError, ValueError):
        # thumbnail is cosmetic, never fatal
        return False
    return rc == 0 and os.path.isfile(thumb_path)
