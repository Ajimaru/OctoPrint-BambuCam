"""Tests for octoprint_bambucam.gcode_thumb."""

import os

from octoprint_bambucam.gcode_thumb import (
    gcode_thumb_source,
    gcode_thumb_source_by_stem,
    write_gcode_thumbnail,
)


def _make_connector_thumb(tmp_path, job_dir):
    """Create the bambucam + bambu_connector thumbs layout under tmp_path.

    Returns (plugin_data_folder, plate_png_path).
    """
    data = tmp_path / "data"
    plugin_data = data / "bambucam"
    plugin_data.mkdir(parents=True)
    plate_dir = data / "bambu_connector" / "thumbs" / job_dir
    plate_dir.mkdir(parents=True)
    plate = plate_dir / "plate_1.png"
    plate.write_bytes(b"\x89PNG\r\n")
    return str(plugin_data), str(plate)


class TestSource:
    """gcode_thumb_source / _by_stem."""

    def test_exact_name_found(self, tmp_path):
        """An exact gcode file name resolves its plate_1.png."""
        plugin_data, plate = _make_connector_thumb(
            tmp_path, "OctoPrint-Upload_red.gcode.3mf"
        )
        got = gcode_thumb_source(plugin_data, "OctoPrint-Upload_red.gcode.3mf")
        assert got == plate

    def test_missing_returns_none(self, tmp_path):
        """A job with no preview returns None."""
        plugin_data, _ = _make_connector_thumb(tmp_path, "other.gcode.3mf")
        assert gcode_thumb_source(plugin_data, "nope.gcode.3mf") is None

    def test_empty_name_returns_none(self, tmp_path):
        """An empty gcode name returns None."""
        plugin_data, _ = _make_connector_thumb(tmp_path, "x.gcode.3mf")
        assert gcode_thumb_source(plugin_data, "") is None

    def test_traversal_rejected(self, tmp_path):
        """A traversing name can't escape the connector thumbs dir."""
        plugin_data, _ = _make_connector_thumb(tmp_path, "x.gcode.3mf")
        assert gcode_thumb_source(plugin_data, "../../etc/passwd") is None

    def test_by_stem_matches_sanitized(self, tmp_path):
        """A print-id stem matches a connector folder by normalized name.

        ``A1+Toolbox`` (connector) ↔ ``A1_Toolbox`` (print-id stem).
        """
        plugin_data, plate = _make_connector_thumb(
            tmp_path, "A1+Toolbox_TPU_20m44s.gcode.3mf"
        )
        got = gcode_thumb_source_by_stem(plugin_data, "A1_Toolbox_TPU_20m44s")
        assert got == plate

    def test_by_stem_no_match(self, tmp_path):
        """No matching folder returns None."""
        plugin_data, _ = _make_connector_thumb(tmp_path, "foo.gcode.3mf")
        assert gcode_thumb_source_by_stem(plugin_data, "bar") is None

    def test_by_stem_no_connector_dir(self, tmp_path):
        """A missing connector thumbs dir returns None (not crash)."""
        plugin_data = tmp_path / "data" / "bambucam"
        plugin_data.mkdir(parents=True)
        assert gcode_thumb_source_by_stem(str(plugin_data), "x") is None


class TestWrite:
    """write_gcode_thumbnail."""

    def test_writes_jpg(self, tmp_path):
        """A successful ffmpeg run reports success and the -i is the PNG."""
        seen = {}

        def runner(cmd, _to, _cb=None, _cancel=None):
            seen["cmd"] = cmd
            with open(cmd[-1], "wb") as fh:
                fh.write(b"jpg")
            return 0, ""

        thumb = str(tmp_path / "out.thumb.jpg")
        ok = write_gcode_thumbnail(
            "/usr/bin/ffmpeg", "/src/plate_1.png", thumb, runner, 1800
        )
        assert ok is True
        assert os.path.exists(thumb)
        cmd = seen["cmd"]
        assert cmd[cmd.index("-i") + 1] == "/src/plate_1.png"

    def test_no_ffmpeg_returns_false(self, tmp_path):
        """Missing ffmpeg path returns False without calling the runner."""
        called = []
        ok = write_gcode_thumbnail(
            "",
            "/src.png",
            str(tmp_path / "t.jpg"),
            lambda *a: called.append(a) or (0, ""),
            1800,
        )
        assert ok is False
        assert called == []

    def test_runner_failure_returns_false(self, tmp_path):
        """A non-zero ffmpeg exit returns False."""
        ok = write_gcode_thumbnail(
            "/usr/bin/ffmpeg",
            "/src.png",
            str(tmp_path / "t.jpg"),
            lambda *a: (1, "boom"),
            1800,
        )
        assert ok is False
