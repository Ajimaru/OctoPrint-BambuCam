"""Storage layout and path-safety helpers for the render pipeline.

The render feature keeps its working files under the plugin data folder
(``<plugin_data>/render/``) split into the directories the safety rules
(plan §3.6/§6) require: raw chunks, work, thumbs, trash, metadata. The final
``.mp4`` is **not** stored here — it lands in OctoPrint's timelapse folder.

``RenderPaths`` centralizes building those directories and re-asserting that
every derived path stays inside its base (realpath containment), reusing the
``paths.sanitize_filename`` rules for the print-id and chunk names.
"""

import os
import re
from typing import Optional

from .paths import FALLBACK_PRINT_STEM, MAX_COLLISION, sanitize_filename

RENDER_ROOT = "render"
RAW_CHUNKS_DIRNAME = "raw/chunks"
WORK_DIRNAME = "work"
THUMBS_DIRNAME = "thumbs"
TRASH_DIRNAME = "trash"
METADATA_DIRNAME = "metadata"

ORDER_FILENAME = "order.json"
JOBS_FILENAME = "jobs.json"
RENDERED_MARKER_FILENAME = "rendered.json"

_PRINT_ID_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}_[0-9]{4}__[\w.-]+$")


def build_print_id(when: str, gcode_name: Optional[str]) -> str:
    """Build a ``<YYYY-MM-DD_HHMM>__<gcode-stem>`` print-id (plan §Opt. 5).

    ``when`` is the real PrintDone wall-clock time as ``"YYYY-MM-DD HH:MM"``
    (never the SD clock). ``gcode_name`` is the print's file name; its stem is
    sanitized via OctoPrint's helper and falls back to
    :data:`FALLBACK_PRINT_STEM` when missing/unsafe. The result is
    filesystem- and URL-safe and sorts chronologically as a plain string.
    """
    date_part = _date_token(when)
    stem = _gcode_stem(gcode_name)
    return f"{date_part}__{stem}"


def _date_token(when: str) -> str:
    """Turn ``"YYYY-MM-DD HH:MM"`` into the ``YYYY-MM-DD_HHMM`` id token."""
    match = re.match(r"(\d{4}-\d{2}-\d{2})[ T](\d{2}):(\d{2})", when or "")
    if not match:
        return "0000-00-00_0000"
    return f"{match.group(1)}_{match.group(2)}{match.group(3)}"


def _gcode_stem(gcode_name: Optional[str]) -> str:
    """Sanitized stem of a gcode file name, or the safe fallback.

    ``sanitize_filename`` keeps characters that are filesystem-safe but not in
    the print-id grammar (e.g. ``+`` in ``A1+Toolbox``). Since the stem becomes
    part of a print-id validated against ``[\\w.-]+``, any character outside
    that class is collapsed to ``_`` — otherwise ``is_valid_print_id`` would
    reject the built id and the whole group would silently never be created.
    """
    base = os.path.basename(gcode_name or "")
    stem = os.path.splitext(base)[0]
    safe = sanitize_filename(stem)
    safe_stem = os.path.splitext(safe)[0]
    safe_stem = _STEM_ILLEGAL_RE.sub("_", safe_stem)
    if not safe or not safe_stem:
        return FALLBACK_PRINT_STEM
    return safe_stem


# Characters allowed in the print-id stem are exactly the grammar's [\w.-];
# anything else (``+``, spaces the sanitizer may keep, …) becomes ``_``.
_STEM_ILLEGAL_RE = re.compile(r"[^\w.-]")


def is_valid_print_id(print_id: str) -> bool:
    """True when ``print_id`` matches the canonical print-id shape.

    Guards API inputs and directory scans against traversal: a value with a
    slash, ``..`` or any character outside the print-id grammar is rejected.
    """
    return bool(print_id) and bool(_PRINT_ID_RE.match(print_id))


class RenderPaths:
    """Resolve and contain the render pipeline's storage directories.

    Construct with the plugin data folder (``get_plugin_data_folder()``).
    Each accessor returns an absolute path that is guaranteed to live inside
    ``<data>/render``; :meth:`ensure_dirs` creates them once at startup.
    """

    def __init__(self, data_folder: str):
        self._root = os.path.join(data_folder, RENDER_ROOT)

    @property
    def root(self) -> str:
        """The ``<data>/render`` base directory."""
        return self._root

    @property
    def raw_chunks_dir(self) -> str:
        """Directory holding each group's downloaded ``/ipcam`` chunks."""
        return os.path.join(self._root, RAW_CHUNKS_DIRNAME)

    @property
    def work_dir(self) -> str:
        """Scratch directory for in-progress concat/render output."""
        return os.path.join(self._root, WORK_DIRNAME)

    @property
    def thumbs_dir(self) -> str:
        """Directory holding the per-group raw-preview JPEGs."""
        return os.path.join(self._root, THUMBS_DIRNAME)

    @property
    def trash_dir(self) -> str:
        """Directory holding soft-deleted (discarded) groups."""
        return os.path.join(self._root, TRASH_DIRNAME)

    @property
    def metadata_dir(self) -> str:
        """Directory holding the persisted pipeline metadata."""
        return os.path.join(self._root, METADATA_DIRNAME)

    @property
    def jobs_file(self) -> str:
        """Path to the persisted render-job registry file."""
        return os.path.join(self.metadata_dir, JOBS_FILENAME)

    def ensure_dirs(self) -> None:
        """Create all render directories (idempotent)."""
        for path in (
            self.raw_chunks_dir,
            self.work_dir,
            self.thumbs_dir,
            self.trash_dir,
            self.metadata_dir,
        ):
            os.makedirs(path, exist_ok=True)

    def group_dir(self, print_id: str) -> Optional[str]:
        """Absolute, contained ``raw/chunks/<print-id>/`` path, or ``None``.

        Returns ``None`` when ``print_id`` is not a valid print-id or escapes
        the raw-chunks base.
        """
        if not is_valid_print_id(print_id):
            return None
        path = os.path.join(self.raw_chunks_dir, print_id)
        if not is_contained(path, self.raw_chunks_dir):
            return None
        return path

    def order_file(self, print_id: str) -> Optional[str]:
        """``order.json`` path for a group, or ``None`` when contained fails."""
        group = self.group_dir(print_id)
        if group is None:
            return None
        return os.path.join(group, ORDER_FILENAME)

    def rendered_marker(self, print_id: str) -> Optional[str]:
        """``rendered.json`` marker path for a group, or ``None``."""
        group = self.group_dir(print_id)
        if group is None:
            return None
        return os.path.join(group, RENDERED_MARKER_FILENAME)

    def thumb_file(self, print_id: str) -> Optional[str]:
        """Contained ``thumbs/<print-id>.jpg`` path, or ``None``."""
        if not is_valid_print_id(print_id):
            return None
        path = os.path.join(self.thumbs_dir, f"{print_id}.jpg")
        if not is_contained(path, self.thumbs_dir):
            return None
        return path

    def chunk_path(self, print_id: str, chunk_name: str) -> Optional[str]:
        """Contained path to a chunk file inside a group, or ``None``.

        ``chunk_name`` is rejected if it contains a path separator or escapes
        the group directory, so a tampered ``order.json``/API payload can never
        point ffmpeg outside the group.
        """
        group = self.group_dir(print_id)
        if group is None:
            return None
        base = os.path.basename(chunk_name or "")
        if not base or base != chunk_name:
            return None
        path = os.path.join(group, base)
        if not is_contained(path, group):
            return None
        return path


def is_contained(dest: str, basefolder: str) -> bool:
    """True when ``dest`` resolves inside ``basefolder`` (realpath check)."""
    base = os.path.realpath(basefolder)
    real = os.path.realpath(dest)
    return real == base or real.startswith(base + os.sep)


def collision_safe(basefolder: str, stem: str, ext: str) -> Optional[str]:
    """Return a non-clobbering filename, appending ``-N`` (cap reused).

    Mirrors ``timelapse_ops._collision_safe`` so the render output lands in
    OctoPrint's timelapse folder under a unique name.
    """
    name = f"{stem}{ext}"
    if not os.path.exists(os.path.join(basefolder, name)):
        return name
    for i in range(1, MAX_COLLISION + 1):
        name = f"{stem}-{i}{ext}"
        if not os.path.exists(os.path.join(basefolder, name)):
            return name
    return None
