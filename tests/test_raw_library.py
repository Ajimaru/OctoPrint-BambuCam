"""Tests for octoprint_bambucam.raw_library."""

# pylint: disable=protected-access
# redefined-outer-name is the standard pytest fixture-injection pattern (a
# fixture and the test param share a name); disabled file-wide as elsewhere.
# pylint: disable=redefined-outer-name

import json
import logging
import os
import shutil

import pytest

from octoprint_bambucam.raw_library import (
    STATE_CHUNKS_READY,
    STATE_INCOMPLETE,
    STATE_RENDERED,
    RawLibrary,
)
from octoprint_bambucam.render_paths import RenderPaths


class FakeProbe:
    """Stand-in FfprobeRunner returning canned metadata per chunk."""

    def __init__(self, available=True, duration=6.0):
        self._available = available
        self._duration = duration

    def available(self):
        """Report configured state."""
        return self._available

    def probe(self, path):  # pylint: disable=unused-argument
        """Return fixed metadata for any chunk."""
        return {
            "duration": self._duration,
            "width": 1680,
            "height": 1080,
            "fps": 30.0,
            "codec": "mjpeg",
            "size": 100,
        }


@pytest.fixture()
def logger():
    """Return a test-scoped logger."""
    return logging.getLogger("test.lib")


@pytest.fixture()
def paths(tmp_path):
    """Return a RenderPaths with its directories created."""
    p = RenderPaths(str(tmp_path))
    p.ensure_dirs()
    return p


def _make_group(paths, print_id, chunk_files, order=True, missing=()):
    """Create a group dir with chunk files and an optional order.json."""
    group = paths.group_dir(print_id)
    os.makedirs(group, exist_ok=True)
    for name in chunk_files:
        if name not in missing:
            with open(os.path.join(group, name), "wb") as fh:
                fh.write(b"x" * 100)
    if order:
        items = [
            {"file": n, "slot": i, "mdtm": None, "included": True}
            for i, n in enumerate(chunk_files)
        ]
        with open(paths.order_file(print_id), "w", encoding="utf-8") as fh:
            json.dump(items, fh)


def _mark_rendered(paths, print_id):
    """Write a rendered.json marker like the render worker does."""
    payload = {
        "rendered_at": "2026-07-04 14:48",
        "output": "out.mp4",
        "preset": "fast_720p",
    }
    with open(paths.rendered_marker(print_id), "w", encoding="utf-8") as fh:
        json.dump(payload, fh)


class TestScan:
    """scan() builds group rows with states and metadata."""

    def test_complete_group_is_ready(self, logger, paths):
        """A group whose order chunks all exist is chunks_ready."""
        pid = "2026-06-23_1432__gearbox"
        _make_group(paths, pid, ["a.avi", "b.avi"])
        lib = RawLibrary(logger, paths, FakeProbe())
        groups = lib.scan()
        assert len(groups) == 1
        g = groups[0]
        assert g["state"] == STATE_CHUNKS_READY
        assert g["chunk_count"] == 2
        assert g["duration"] == 12.0
        assert g["width"] == 1680
        assert g["size"] == 200

    def test_missing_chunk_is_incomplete(self, logger, paths):
        """A group with a missing ordered chunk is incomplete."""
        pid = "2026-06-23_1432__gearbox"
        _make_group(paths, pid, ["a.avi", "b.avi"], missing=["b.avi"])
        lib = RawLibrary(logger, paths, FakeProbe())
        g = lib.scan()[0]
        assert g["state"] == STATE_INCOMPLETE

    def test_no_order_json_lists_and_incomplete(self, logger, paths):
        """A group without order.json lists files and is incomplete."""
        pid = "2026-06-23_1432__gearbox"
        _make_group(paths, pid, ["a.avi"], order=False)
        lib = RawLibrary(logger, paths, FakeProbe())
        g = lib.scan()[0]
        assert g["state"] == STATE_INCOMPLETE
        assert g["chunk_count"] == 1

    def test_invalid_order_json_treated_as_missing(self, logger, paths):
        """A corrupt order.json falls back to listing (incomplete)."""
        pid = "2026-06-23_1432__gearbox"
        _make_group(paths, pid, ["a.avi"], order=False)
        with open(paths.order_file(pid), "w", encoding="utf-8") as fh:
            fh.write("not json")
        lib = RawLibrary(logger, paths, FakeProbe())
        g = lib.scan()[0]
        assert g["state"] == STATE_INCOMPLETE

    def test_non_print_id_dirs_ignored(self, logger, paths):
        """A directory that is not a valid print-id is skipped."""
        os.makedirs(os.path.join(paths.raw_chunks_dir, "random"))
        lib = RawLibrary(logger, paths, FakeProbe())
        assert lib.scan() == []

    def test_empty_group_dropped(self, logger, paths):
        """A group dir with no chunk files yields no row."""
        pid = "2026-06-23_1432__gearbox"
        os.makedirs(paths.group_dir(pid))
        lib = RawLibrary(logger, paths, FakeProbe())
        assert lib.scan() == []

    def test_probe_unavailable_skips_meta(self, logger, paths):
        """With ffprobe unavailable, size is summed but duration is None."""
        pid = "2026-06-23_1432__gearbox"
        _make_group(paths, pid, ["a.avi"])
        lib = RawLibrary(logger, paths, FakeProbe(available=False))
        g = lib.scan()[0]
        assert g["duration"] is None
        assert g["size"] == 100

    def test_appledouble_sidecars_ignored(self, logger, paths):
        """``._name.avi`` resource-fork stubs are not counted as chunks."""
        pid = "2026-06-23_1432__gearbox"
        _make_group(paths, pid, ["a.avi"], order=False)
        group = paths.group_dir(pid)
        with open(os.path.join(group, "._a.avi"), "wb") as fh:
            fh.write(b"stub")
        lib = RawLibrary(logger, paths, FakeProbe())
        g = lib.scan()[0]
        assert g["chunk_count"] == 1
        assert g["chunks"][0]["file"] == "a.avi"

    def test_rendered_marker_wins(self, logger, paths):
        """A group with a rendered.json marker reports state rendered."""
        pid = "2026-06-23_1432__gearbox"
        _make_group(paths, pid, ["a.avi", "b.avi"])
        _mark_rendered(paths, pid)
        lib = RawLibrary(logger, paths, FakeProbe())
        g = lib.scan()[0]
        assert g["state"] == STATE_RENDERED
        assert g["rendered_at"] == "2026-07-04 14:48"
        assert g["rendered_output"] == "out.mp4"

    def test_invalid_rendered_marker_ignored(self, logger, paths):
        """A corrupt rendered.json leaves the group chunks_ready."""
        pid = "2026-06-23_1432__gearbox"
        _make_group(paths, pid, ["a.avi"])
        with open(paths.rendered_marker(pid), "w", encoding="utf-8") as fh:
            fh.write("not json")
        lib = RawLibrary(logger, paths, FakeProbe())
        g = lib.scan()[0]
        assert g["state"] == STATE_CHUNKS_READY
        assert g["rendered_at"] is None

    def test_has_thumb_flag(self, logger, paths):
        """has_thumb reflects an existing thumbnail file."""
        pid = "2026-06-23_1432__gearbox"
        _make_group(paths, pid, ["a.avi"])
        with open(paths.thumb_file(pid), "wb") as fh:
            fh.write(b"jpeg")
        lib = RawLibrary(logger, paths, FakeProbe())
        assert lib.scan()[0]["has_thumb"] is True


class TestCache:
    """groups()/get()/forget() operate on the cached scan result."""

    def test_get_and_forget(self, logger, paths):
        """A scanned group is retrievable then forgettable."""
        pid = "2026-06-23_1432__gearbox"
        _make_group(paths, pid, ["a.avi"])
        lib = RawLibrary(logger, paths, FakeProbe())
        lib.scan()
        assert lib.get(pid) is not None
        lib.forget(pid)
        assert lib.get(pid) is None

    def test_groups_sorted_newest_first(self, logger, paths):
        """groups() sorts by print-id descending."""
        _make_group(paths, "2026-06-23_1000__a", ["a.avi"])
        _make_group(paths, "2026-06-23_1200__b", ["a.avi"])
        lib = RawLibrary(logger, paths, FakeProbe())
        ids = [g["print_id"] for g in lib.scan()]
        assert ids == ["2026-06-23_1200__b", "2026-06-23_1000__a"]

    def test_refresh_picks_up_rendered_marker(self, logger, paths):
        """refresh() rebuilds one cached group in place."""
        pid = "2026-06-23_1432__gearbox"
        _make_group(paths, pid, ["a.avi"])
        lib = RawLibrary(logger, paths, FakeProbe())
        lib.scan()
        assert lib.get(pid)["state"] == STATE_CHUNKS_READY
        _mark_rendered(paths, pid)
        lib.refresh(pid)
        assert lib.get(pid)["state"] == STATE_RENDERED

    def test_refresh_drops_vanished_group(self, logger, paths):
        """refresh() forgets a group whose directory is gone."""
        pid = "2026-06-23_1432__gearbox"
        _make_group(paths, pid, ["a.avi"])
        lib = RawLibrary(logger, paths, FakeProbe())
        lib.scan()
        shutil.rmtree(paths.group_dir(pid))
        lib.refresh(pid)
        assert lib.get(pid) is None


class TestInterruptedHarvest:
    """A group holding only ``.part`` temps stays visible as incomplete."""

    @staticmethod
    def _make_part_group(paths, print_id, parts, chunks=()):
        group = paths.group_dir(print_id)
        os.makedirs(group, exist_ok=True)
        for name in parts:
            with open(os.path.join(group, name), "wb") as fh:
                fh.write(b"x" * 50)
        for name in chunks:
            with open(os.path.join(group, name), "wb") as fh:
                fh.write(b"x" * 100)
        return group

    def test_part_only_group_is_incomplete(self, logger, paths):
        """Only a .part on disk: the group is listed, not dropped."""
        pid = "2026-09-09_1817__cover"
        self._make_part_group(paths, pid, ["ipcam-record.1.avi.part"])
        lib = RawLibrary(logger, paths, FakeProbe())
        groups = lib.scan()
        assert len(groups) == 1
        group = groups[0]
        assert group["state"] == STATE_INCOMPLETE
        assert group["chunk_count"] == 1
        chunk = group["chunks"][0]
        # named for the footage it was reaching for, but never present:
        # partial bytes must not look renderable
        assert chunk["file"] == "ipcam-record.1.avi"
        assert chunk["present"] is False
        assert chunk["partial_size"] == 50
        # no present chunk to probe, so no aggregate metadata
        assert group["size"] == 0
        assert group["duration"] is None

    def test_part_beside_finished_chunk_keeps_order(self, logger, paths):
        """A finished chunk still drives the group; the .part is ignored."""
        pid = "2026-09-09_1913__rocket"
        self._make_part_group(
            paths, pid, ["ipcam-record.2.avi.part"], chunks=["a.avi"]
        )
        _make_group(paths, pid, ["a.avi"])
        lib = RawLibrary(logger, paths, FakeProbe())
        group = lib.scan()[0]
        assert group["state"] == STATE_CHUNKS_READY
        assert [c["file"] for c in group["chunks"]] == ["a.avi"]

    def test_empty_group_still_dropped(self, logger, paths):
        """A group with neither chunk nor .part is not listed at all."""
        pid = "2026-09-09_1817__cover"
        os.makedirs(paths.group_dir(pid), exist_ok=True)
        lib = RawLibrary(logger, paths, FakeProbe())
        assert lib.scan() == []

    def test_unrelated_part_is_not_a_chunk(self, logger, paths):
        """A .part whose stem is not chunk-shaped does not create a group."""
        pid = "2026-09-09_1817__cover"
        self._make_part_group(paths, pid, ["notes.txt.part"])
        lib = RawLibrary(logger, paths, FakeProbe())
        assert lib.scan() == []
