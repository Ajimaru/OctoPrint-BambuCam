"""Footage registry for the harvested ``/ipcam`` chunk groups.

Each print's raw chunks live under ``raw/chunks/<print-id>/`` with an
``order.json`` listing their concat order and per-chunk inclusion default
(plan §3.2/§3.3). :class:`RawLibrary` scans those directories, ffprobes the
group once, caches the metadata, and reports a per-group state the UI lists.

Group states (one vocabulary, plan §3.3/§3.5):

``incomplete``
    Download ran but did not finish (a partial harvest); shown with a warning
    badge and manual harvest/discard buttons.
``chunks_ready``
    All chunks downloaded and probed; eligible for "Concat + Render".
``rendered``
    A ``rendered.json`` marker exists (written by the render worker on
    success). The chunks stay on disk, so the group remains listed with a
    "Rendered" badge and can be re-rendered or discarded.

``rendering``/``failed`` are owned by the render queue, not the scan.
"""

import json
import logging
import os
from typing import Optional

from . import render_paths
from .ffprobe import FfprobeError

STATE_INCOMPLETE = "incomplete"
STATE_CHUNKS_READY = "chunks_ready"
STATE_RENDERED = "rendered"

CHUNK_EXTENSIONS = (".avi",)

# Suffix ``ftp.download`` gives its in-progress temp file. A group holding
# only these was interrupted mid-harvest (plan §3.3 ``incomplete``).
PART_SUFFIX = ".part"


class RawLibrary:
    """Scan and cache the harvested chunk groups under ``raw/chunks/``.

    Construct with the :class:`render_paths.RenderPaths` and an
    :class:`ffprobe.FfprobeRunner`. :meth:`scan` rebuilds the in-memory group
    map; :meth:`groups` returns the cached, UI-shaped list without re-probing.
    """

    def __init__(self, logger: logging.Logger, paths, probe):
        self._logger = logger
        self._paths = paths
        self._probe = probe
        self._groups: dict = {}

    def scan(self) -> list:
        """Rescan ``raw/chunks/`` and return the fresh group list.

        Each directory whose name is a valid print-id becomes one group. The
        group is ``chunks_ready`` when its ``order.json`` lists chunks that all
        exist on disk, else ``incomplete``. ffprobe metadata is cached per
        group so :meth:`groups` is cheap.
        """
        base = self._paths.raw_chunks_dir
        groups: dict = {}
        for print_id in self._list_group_ids(base):
            group = self._build_group(print_id)
            if group is not None:
                groups[print_id] = group
        self._groups = groups
        return self.groups()

    def groups(self) -> list:
        """Return the cached groups as a list sorted newest-first."""
        return sorted(
            self._groups.values(),
            key=lambda g: g["print_id"],
            reverse=True,
        )

    def get(self, print_id: str) -> Optional[dict]:
        """Return one cached group dict, or ``None`` if unknown."""
        return self._groups.get(print_id)

    def forget(self, print_id: str) -> None:
        """Drop a group from the cache (after a discard/cleanup)."""
        self._groups.pop(print_id, None)

    def refresh(self, print_id: str) -> Optional[dict]:
        """Re-scan a single group in place (e.g. after a render marked it).

        Drops the group from the cache when its directory is gone; returns the
        rebuilt group dict (or ``None``).
        """
        group = self._build_group(print_id)
        if group is None:
            self._groups.pop(print_id, None)
        else:
            self._groups[print_id] = group
        return group

    def _list_group_ids(self, base: str) -> list:
        try:
            entries = os.listdir(base)
        except OSError:
            return []
        ids = []
        for entry in entries:
            if not render_paths.is_valid_print_id(entry):
                continue
            if os.path.isdir(os.path.join(base, entry)):
                ids.append(entry)
        return ids

    def _build_group(self, print_id: str) -> Optional[dict]:
        group_dir = self._paths.group_dir(print_id)
        if group_dir is None:
            return None
        order = self._read_order(print_id)
        chunks = self._resolve_chunks(group_dir, order)
        if not chunks:
            # No finished chunk, but a ``.part`` means a harvest was cut short
            # (an OctoPrint restart or a crash mid-transfer). Report the group
            # as incomplete instead of hiding it, so the tab still offers
            # re-harvest/discard rather than leaving an invisible directory
            # holding on to the partial download.
            chunks = _partial_chunks(group_dir)
            if not chunks:
                return None
        present = [c for c in chunks if c["present"]]
        complete = order is not None and all(c["present"] for c in chunks)
        marker = self._read_rendered_marker(print_id)
        if marker is not None:
            state = STATE_RENDERED
        elif complete:
            state = STATE_CHUNKS_READY
        else:
            state = STATE_INCOMPLETE
        meta = self._group_meta(group_dir, present)
        return {
            "print_id": print_id,
            "state": state,
            "chunks": chunks,
            "chunk_count": len(chunks),
            "size": meta["size"],
            "duration": meta["duration"],
            "width": meta["width"],
            "height": meta["height"],
            "fps": meta["fps"],
            "codec": meta["codec"],
            "has_thumb": self._has_thumb(print_id),
            "rendered_at": (marker or {}).get("rendered_at"),
            "rendered_output": (marker or {}).get("output"),
        }

    def _read_rendered_marker(self, print_id: str) -> Optional[dict]:
        """Read ``rendered.json``; ``None`` when missing/invalid."""
        marker = self._paths.rendered_marker(print_id)
        if marker is None or not os.path.isfile(marker):
            return None
        try:
            with open(marker, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def _read_order(self, print_id: str) -> Optional[list]:
        """Read and validate ``order.json``; ``None`` when missing/invalid."""
        order_file = self._paths.order_file(print_id)
        if order_file is None or not os.path.isfile(order_file):
            return None
        try:
            with open(order_file, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            self._logger.warning("invalid order.json for %s", print_id)
            return None
        if not isinstance(data, list):
            return None
        return data

    def _resolve_chunks(self, group_dir: str, order: Optional[list]) -> list:
        """Build the per-chunk list, ordered by ``order.json`` when present.

        Falls back to a plain directory listing (sorted) when ``order.json`` is
        absent, marking the group incomplete. Each entry reports whether its
        file is currently present on disk.
        """
        if order is not None:
            return self._chunks_from_order(group_dir, order)
        return self._chunks_from_listing(group_dir)

    def _chunks_from_order(self, group_dir: str, order: list) -> list:
        chunks = []
        for item in order:
            if not isinstance(item, dict):
                continue
            name = os.path.basename(str(item.get("file", "")))
            if not _is_chunk(name):
                continue
            path = os.path.join(group_dir, name)
            chunks.append(
                {
                    "file": name,
                    "slot": item.get("slot"),
                    "mdtm": item.get("mdtm"),
                    "included": bool(item.get("included", True)),
                    "present": os.path.isfile(path),
                    "size": _safe_size(path),
                }
            )
        return chunks

    def _chunks_from_listing(self, group_dir: str) -> list:
        try:
            entries = sorted(os.listdir(group_dir))
        except OSError:
            return []
        chunks = []
        for name in entries:
            if not _is_chunk(name):
                continue
            path = os.path.join(group_dir, name)
            if not os.path.isfile(path):
                continue
            chunks.append(
                {
                    "file": name,
                    "slot": None,
                    "mdtm": None,
                    "included": True,
                    "present": True,
                    "size": _safe_size(path),
                }
            )
        return chunks

    def _group_meta(self, group_dir: str, present: list) -> dict:
        """Aggregate group metadata: summed size, probed first present chunk.

        Resolution/codec/fps come from one chunk (all ``/ipcam`` chunks are
        identical, plan §Opt. 15); duration is summed across present chunks.
        """
        size = sum(c["size"] or 0 for c in present)
        meta = {
            "size": size,
            "duration": None,
            "width": None,
            "height": None,
            "fps": None,
            "codec": None,
        }
        if not present or not self._probe.available():
            return meta
        total_duration = 0.0
        have_duration = False
        for chunk in present:
            probed = self._probe_chunk(group_dir, chunk["file"])
            if probed is None:
                continue
            if meta["width"] is None:
                meta["width"] = probed.get("width")
                meta["height"] = probed.get("height")
                meta["fps"] = probed.get("fps")
                meta["codec"] = probed.get("codec")
            if probed.get("duration") is not None:
                total_duration += probed["duration"]
                have_duration = True
        if have_duration:
            meta["duration"] = round(total_duration, 3)
        return meta

    def _probe_chunk(self, group_dir: str, name: str) -> Optional[dict]:
        path = os.path.join(group_dir, name)
        try:
            return self._probe.probe(path)
        except FfprobeError as exc:
            self._logger.debug("ffprobe failed for %s: %s", name, exc.reason)
            return None

    def _has_thumb(self, print_id: str) -> bool:
        thumb = self._paths.thumb_file(print_id)
        return bool(thumb and os.path.isfile(thumb))


def _is_chunk(name: str) -> bool:
    """True for a real chunk file: a chunk extension and not a dot-sidecar.

    Skips AppleDouble/resource-fork stubs (``._name.avi``) the printer's FTP
    sometimes lists, which carry the chunk extension but are not footage.
    """
    if not name or name.startswith("._"):
        return False
    return name.lower().endswith(CHUNK_EXTENSIONS)


def _partial_chunks(group_dir: str) -> list:
    """Describe a cut-short harvest's ``.part`` temps as absent chunks.

    The chunk name is the ``.part`` stem, so the UI names the footage the
    harvest was reaching for. ``present`` is false throughout: the bytes on
    disk are a partial transfer, never renderable, which keeps the group in
    ``incomplete`` and out of the render queue.
    """
    try:
        entries = sorted(os.listdir(group_dir))
    except OSError:
        return []
    chunks = []
    for name in entries:
        if not name.endswith(PART_SUFFIX):
            continue
        stem = name[: -len(PART_SUFFIX)]
        if not _is_chunk(stem):
            continue
        chunks.append(
            {
                "file": stem,
                "slot": None,
                "mdtm": None,
                "included": True,
                "present": False,
                "size": None,
                "partial_size": _safe_size(os.path.join(group_dir, name)),
            }
        )
    return chunks


def _safe_size(path: str) -> Optional[int]:
    try:
        return os.path.getsize(path)
    except OSError:
        return None
