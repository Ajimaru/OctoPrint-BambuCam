"""Filename-safety constants and helpers shared by the plugin modules.

These back the §5.7/§5.8 path rules (sanitize, fallback stem, suffix/collision
caps, disk-space margin) used by both the plugin entry point and the timelapse
transfer mixin.
"""

import re

from octoprint.util.files import sanitize_filename as _op_sanitize_filename

_BAMBU_TS_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})_(\d{2})-(\d{2})-(\d{2})")

DISK_MARGIN_BYTES = 50 * 1024 * 1024
FALLBACK_STEM = "bambu-timelapse"
MAX_SUFFIX_LEN = 32
MAX_COLLISION = 1000


def sanitize_filename(name: str) -> str:
    """Wrap OctoPrint's ``sanitize_filename``, returning ``""`` on rejection.

    OctoPrint's helper *raises* ``ValueError`` on names containing ``/`` or
    ``\\`` rather than stripping them; we treat that as an empty (unsafe) result
    so the caller falls back to a safe default (plan §5.7 #1/#6).
    """
    try:
        return _op_sanitize_filename(name) or ""
    except ValueError:
        return ""


def bambu_sort_key(name: str) -> str:
    """Chronological sort key from a Bambu timelapse name.

    Returns the timestamp digits (``YYYYMMDDHHMMSS``) so names compare in
    capture order as plain strings, or ``""`` when the name has no recognizable
    timestamp (those sort first and never advance the high-water mark).
    """
    m = _BAMBU_TS_RE.search(name or "")
    return "".join(m.groups()) if m else ""
