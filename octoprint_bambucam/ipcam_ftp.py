"""FTPS access to the printer's ``/ipcam`` raw high-res timelapse chunks.

The A1 mini records raw timelapse footage into ``/ipcam`` as a 160-slot ring
buffer of MJPEG ``.avi`` chunks named ``ipcam-record.<dt>.<SLOT>.avi`` (see
``RESEARCH-ipcam-highres-timelapse.md``). :class:`BambuIpcamFtp` reuses the
connect/download/delete machinery of :class:`ftp.BambuTimelapseFtp` and only
swaps the listing directory and adds slot parsing, so the implicit-TLS logic is
not copy-pasted.
"""

import re
from typing import Optional

from .ftp import BambuTimelapseFtp

IPCAM_DIR = "/ipcam"

_SLOT_RE = re.compile(r"\.(\d+)\.avi$", re.IGNORECASE)


class BambuIpcamFtp(BambuTimelapseFtp):
    """List and download the printer's ``/ipcam`` ring-buffer chunks.

    Inherits the single-connection, lock-serialized FTPS session from the
    timelapse service; :meth:`list_ipcam` adds the ``slot`` field parsed from
    the ring-buffer name pattern. ``index`` (a 4-byte counter file) is filtered
    out by the inherited ``.avi`` video filter.
    """

    LIST_DIR = IPCAM_DIR

    def list_ipcam(self) -> list:
        """Return ``[{name, size, date, slot}]`` for the ``/ipcam`` chunks.

        Delegates the directory listing to the inherited
        :meth:`list_timelapses` (which lists ``LIST_DIR`` = ``/ipcam`` here)
        and annotates each entry with its ring-buffer ``slot`` number.
        """
        files = self.list_timelapses()
        for entry in files:
            entry["slot"] = _parse_slot(entry.get("name", ""))
        return files


def _parse_slot(name: str) -> Optional[int]:
    """Parse the ring-buffer slot from an ``ipcam-record.<dt>.<SLOT>.avi`` name.

    Returns the slot integer, or ``None`` when the name does not match the
    pattern (so a stray file does not crash the listing).
    """
    match = _SLOT_RE.search(name or "")
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None
