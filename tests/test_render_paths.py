"""Tests for octoprint_bambucam.render_paths."""

import os

from octoprint_bambucam.render_paths import (
    RenderPaths,
    build_print_id,
    collision_safe,
    is_contained,
    is_valid_print_id,
)


class TestPrintId:
    """build_print_id / is_valid_print_id."""

    def test_build_basic(self):
        """A clean time + gcode name yields the canonical id."""
        pid = build_print_id("2026-06-23 14:32", "gearbox.gcode")
        assert pid == "2026-06-23_1432__gearbox"

    def test_build_missing_gcode_falls_back(self):
        """A missing gcode name uses the fallback stem."""
        pid = build_print_id("2026-06-23 14:32", None)
        assert pid == "2026-06-23_1432__unknown-print"

    def test_build_bad_time(self):
        """An unparsable time uses the zero date token."""
        pid = build_print_id("garbage", "x.gcode")
        assert pid.startswith("0000-00-00_0000__")

    def test_build_stem_with_plus_stays_valid(self):
        """A gcode name with '+' (e.g. A1+Toolbox) must still validate.

        ``sanitize_filename`` keeps '+', but the print-id grammar does not; if
        the built id fails ``is_valid_print_id`` the group is silently never
        created (regression: manual harvest of an 'A1+Toolbox' job).
        """
        pid = build_print_id("2026-07-05 17:10", "A1+Toolbox_TPU_20m44s.gcode")
        assert pid == "2026-07-05_1710__A1_Toolbox_TPU_20m44s"
        assert is_valid_print_id(pid)

    def test_build_stem_only_illegal_chars_still_valid(self):
        """A name of only out-of-grammar characters still yields a valid id."""
        pid = build_print_id("2026-07-05 17:10", "+++.gcode")
        assert is_valid_print_id(pid)

    def test_valid_print_id(self):
        """The canonical shape validates; traversal does not."""
        assert is_valid_print_id("2026-06-23_1432__gearbox")
        assert not is_valid_print_id("../etc")
        assert not is_valid_print_id("2026-06-23_1432/gearbox")
        assert not is_valid_print_id("")


class TestRenderPaths:
    """RenderPaths builds contained directories."""

    def test_ensure_dirs_creates_all(self, tmp_path):
        """ensure_dirs makes every storage directory."""
        paths = RenderPaths(str(tmp_path))
        paths.ensure_dirs()
        for path in (
            paths.raw_chunks_dir,
            paths.work_dir,
            paths.thumbs_dir,
            paths.trash_dir,
            paths.metadata_dir,
        ):
            assert os.path.isdir(path)

    def test_group_dir_valid(self, tmp_path):
        """A valid print-id yields a contained group dir."""
        paths = RenderPaths(str(tmp_path))
        group = paths.group_dir("2026-06-23_1432__gearbox")
        assert group is not None
        assert group.endswith("2026-06-23_1432__gearbox")

    def test_group_dir_rejects_bad_id(self, tmp_path):
        """An invalid print-id yields None."""
        paths = RenderPaths(str(tmp_path))
        assert paths.group_dir("../escape") is None

    def test_order_and_thumb_file(self, tmp_path):
        """order.json and thumb paths derive from a valid id."""
        paths = RenderPaths(str(tmp_path))
        pid = "2026-06-23_1432__gearbox"
        order_file = paths.order_file(pid)
        thumb_file = paths.thumb_file(pid)
        assert order_file is not None and order_file.endswith("order.json")
        assert thumb_file is not None and thumb_file.endswith(pid + ".jpg")
        assert paths.order_file("../x") is None
        assert paths.thumb_file("../x") is None

    def test_chunk_path_contained(self, tmp_path):
        """A plain chunk name resolves inside the group; traversal is None."""
        paths = RenderPaths(str(tmp_path))
        pid = "2026-06-23_1432__gearbox"
        assert paths.chunk_path(pid, "ipcam-record.0.1.avi") is not None
        assert paths.chunk_path(pid, "../../etc/passwd") is None
        assert paths.chunk_path(pid, "sub/dir.avi") is None
        assert paths.chunk_path("../bad", "x.avi") is None


class TestHelpers:
    """is_contained / collision_safe."""

    def test_is_contained(self, tmp_path):
        """Containment holds for children, not for escapes."""
        base = str(tmp_path)
        assert is_contained(os.path.join(base, "a", "b"), base)
        assert not is_contained("/etc/passwd", base)

    def test_collision_safe_free(self, tmp_path):
        """A free name returns unchanged."""
        assert collision_safe(str(tmp_path), "clip", ".mp4") == "clip.mp4"

    def test_collision_safe_appends(self, tmp_path):
        """An existing name gets a -N suffix."""
        (tmp_path / "clip.mp4").write_text("x")
        assert collision_safe(str(tmp_path), "clip", ".mp4") == "clip-1.mp4"
