"""Tests for octoprint_bambucam.ffprobe."""

# pylint: disable=protected-access
# redefined-outer-name is the standard pytest fixture-injection pattern (a
# fixture and the test param share a name); disabled file-wide as elsewhere.
# pylint: disable=redefined-outer-name

import logging

import pytest

from octoprint_bambucam.ffprobe import (
    FfprobeError,
    FfprobeRunner,
    _parse_fps,
    _reduce_probe,
    fallback_ffprobe_path,
)

PROBE_JSON = (
    '{"format": {"duration": "12.5"}, "streams": ['
    '{"codec_type": "audio"},'
    '{"codec_type": "video", "width": 1680, "height": 1080,'
    ' "avg_frame_rate": "30/1", "codec_name": "mjpeg"}]}'
)


@pytest.fixture()
def logger():
    """Return a test-scoped logger."""
    return logging.getLogger("test.ffprobe")


def _stub_runner(returncode, output):
    """Return a runner stub yielding a fixed ``(returncode, output)``."""

    def run(_cmd, _timeout):
        return returncode, output

    return run


class TestProbe:
    """probe() reduces ffprobe JSON to the library fields."""

    def test_probe_reduces_fields(self, logger, tmp_path):
        """A video file is probed into duration/res/fps/codec/size."""
        video = tmp_path / "chunk.avi"
        video.write_bytes(b"x" * 100)
        probe = FfprobeRunner(
            logger,
            ffprobe_path="/bin/ffprobe",
            runner=_stub_runner(0, PROBE_JSON),
        )
        meta = probe.probe(str(video))
        assert meta["duration"] == 12.5
        assert meta["width"] == 1680
        assert meta["height"] == 1080
        assert meta["fps"] == 30.0
        assert meta["codec"] == "mjpeg"
        assert meta["size"] == 100

    def test_probe_unconfigured_raises(self, logger, tmp_path):
        """An empty ffprobe path makes probe() raise no_ffprobe."""
        probe = FfprobeRunner(logger, ffprobe_path="")
        with pytest.raises(FfprobeError) as exc:
            probe.probe(str(tmp_path / "x.avi"))
        assert exc.value.reason == "no_ffprobe"

    def test_probe_missing_file_raises(self, logger):
        """A missing input file raises not_found."""
        probe = FfprobeRunner(logger, ffprobe_path="/bin/ffprobe")
        with pytest.raises(FfprobeError) as exc:
            probe.probe("/nope/missing.avi")
        assert exc.value.reason == "not_found"

    def test_probe_nonzero_exit_raises(self, logger, tmp_path):
        """A non-zero ffprobe exit raises ffprobe_failed."""
        video = tmp_path / "c.avi"
        video.write_bytes(b"x")
        probe = FfprobeRunner(
            logger, ffprobe_path="/bin/ffprobe", runner=_stub_runner(1, "")
        )
        with pytest.raises(FfprobeError) as exc:
            probe.probe(str(video))
        assert exc.value.reason == "ffprobe_failed"

    def test_probe_bad_json_raises(self, logger, tmp_path):
        """Unparsable ffprobe output raises bad_json."""
        video = tmp_path / "c.avi"
        video.write_bytes(b"x")
        probe = FfprobeRunner(
            logger,
            ffprobe_path="/bin/ffprobe",
            runner=_stub_runner(0, "not json"),
        )
        with pytest.raises(FfprobeError) as exc:
            probe.probe(str(video))
        assert exc.value.reason == "bad_json"


class TestStatus:
    """available()/status() report configuration state."""

    def test_available_and_status(self, logger):
        """A set-but-missing path is configured but not executable."""
        probe = FfprobeRunner(logger, ffprobe_path="/nope/ffprobe")
        assert probe.available() is True
        status = probe.status()
        assert status["configured"] is True
        assert status["executable"] is False
        assert status["path"] == "/nope/ffprobe"

    def test_unconfigured_status(self, logger):
        """An empty path is not configured."""
        probe = FfprobeRunner(logger, ffprobe_path=None)
        assert probe.available() is False
        assert probe.status()["configured"] is False


class TestHelpers:
    """Helper functions parse fps and fall back to a sibling ffprobe."""

    def test_parse_fps_fraction(self):
        """A "num/den" rate parses to a float."""
        assert _parse_fps("30000/1001") == 29.97

    def test_parse_fps_zero_den(self):
        """A zero denominator yields None, not a divide error."""
        assert _parse_fps("30/0") is None

    def test_parse_fps_plain(self):
        """A non-fraction rate parses as a plain float."""
        assert _parse_fps("25") == 25.0

    def test_reduce_probe_empty(self):
        """Empty probe data reduces to all-None fields."""
        meta = _reduce_probe({})
        assert meta["width"] is None
        assert meta["codec"] is None

    def test_fallback_next_to_ffmpeg(self, tmp_path):
        """ffprobe is found next to ffmpeg by name substitution."""
        ffmpeg = tmp_path / "ffmpeg"
        ffmpeg.write_text("#!/bin/sh\n")
        ffprobe = tmp_path / "ffprobe"
        ffprobe.write_text("#!/bin/sh\n")
        assert fallback_ffprobe_path(str(ffmpeg)) == str(ffprobe)

    def test_fallback_empty(self):
        """An empty ffmpeg path yields no fallback."""
        assert fallback_ffprobe_path("") is None

    def test_fallback_no_sibling(self, tmp_path):
        """A missing sibling ffprobe yields None."""
        ffmpeg = tmp_path / "ffmpeg"
        ffmpeg.write_text("x")
        assert fallback_ffprobe_path(str(ffmpeg)) is None
