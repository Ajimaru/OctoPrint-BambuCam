"""Render presets (scale/speed/crf/x264-preset) for the encode step.

The four presets are fixed by the plan (§Opt. 9): three downscaling speed
profiles plus an ``original`` archive profile that keeps the full ``/ipcam``
resolution. ``speed`` is a playback multiplier (``N×``) realized as
``setpts=(1/N)*PTS``; ``scale`` is the target width with ``-2`` height (kept
even, aspect preserved).
"""

from typing import Optional

PRESETS = {
    "fast_720p": {
        "label": "Fast 720p",
        "scale": 1280,
        "speed": 5.0,
        "crf": 26,
        "x264_preset": "veryfast",
    },
    "medium_1080p": {
        "label": "Medium 1080p",
        "scale": 1920,
        "speed": 2.0,
        "crf": 23,
        "x264_preset": "medium",
    },
    "quality_1080p": {
        "label": "Quality 1080p",
        "scale": 1920,
        "speed": 1.0,
        "crf": 20,
        "x264_preset": "slow",
    },
    "original": {
        "label": "Original (archive, slow)",
        "scale": 1680,
        "speed": 1.0,
        "crf": 18,
        "x264_preset": "slow",
    },
}

DEFAULT_PRESET = "fast_720p"


def get_preset(name: Optional[str]) -> dict:
    """Return the preset dict for ``name``, falling back to the default."""
    return PRESETS.get(name or "", PRESETS[DEFAULT_PRESET])


def preset_names() -> list:
    """Return the preset keys in display order."""
    return list(PRESETS.keys())


def build_vf(preset: dict) -> str:
    """Build the ffmpeg ``-vf`` filter string for a preset.

    Combines the speed (``setpts``) and downscale (``scale``) filters. The
    speed factor is ``1/N`` so a ``5×`` preset plays five times faster.
    """
    speed = preset.get("speed") or 1.0
    scale = preset.get("scale") or 1280
    setpts = round(1.0 / speed, 6)
    return f"setpts={setpts}*PTS,scale={scale}:-2"
