"""Tests for octoprint_bambucam.transcode.TimelapseTranscoder."""

# pylint: disable=protected-access,redefined-outer-name,too-few-public-methods

import logging
import os
import stat

import pytest

from octoprint_bambucam import transcode as tc
from octoprint_bambucam.transcode import (
    TimelapseTranscoder,
    TranscodeError,
    _iter_ffmpeg_lines,
)


@pytest.fixture()
def logger():
    """Provide a logger for the transcoder under test."""
    return logging.getLogger("test.transcode")


def _writing_runner(out_bytes=b"mp4data", rc=0, err=""):
    """A fake ffmpeg runner that writes the output file (last argv) and rc."""

    def run(cmd, _timeout, _progress_cb=None):
        if rc == 0:
            out = cmd[-1]
            with open(out, "wb") as fh:
                fh.write(out_bytes)
        return rc, err

    return run


class TestAvailable:
    """Tests for TimelapseTranscoder.available()."""

    def test_no_ffmpeg_unavailable(self, logger):
        """available() is False when no ffmpeg path is configured."""
        t = TimelapseTranscoder(logger, ffmpeg_path=None)
        assert t.available() is False

    def test_empty_path_unavailable(self, logger):
        """available() is False for a blank ffmpeg path."""
        t = TimelapseTranscoder(logger, ffmpeg_path="   ")
        assert t.available() is False

    def test_path_set_available(self, logger):
        """available() is True once an ffmpeg path is set."""
        t = TimelapseTranscoder(logger, ffmpeg_path="/usr/bin/ffmpeg")
        assert t.available() is True


class TestStatus:
    """Tests for TimelapseTranscoder.status()."""

    def test_status_not_configured(self, logger):
        """status() reports unconfigured when no path is set."""
        s = TimelapseTranscoder(logger, ffmpeg_path=None).status()
        assert s == {"path": "", "configured": False, "executable": False}

    def test_status_configured_but_missing(self, logger):
        """status() reports configured but not executable if binary missing."""
        s = TimelapseTranscoder(logger, ffmpeg_path="/nope/ffmpeg").status()
        assert s["configured"] is True
        assert s["executable"] is False
        assert s["path"] == "/nope/ffmpeg"

    def test_status_executable(self, logger, tmp_path):
        """status() reports executable for an existing executable binary."""
        fake = tmp_path / "ffmpeg"
        fake.write_text("#!/bin/sh\n")
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
        s = TimelapseTranscoder(logger, ffmpeg_path=str(fake)).status()
        assert s["configured"] is True
        assert s["executable"] is True
        assert s["path"] == str(fake)


class TestTranscode:
    """Tests for TimelapseTranscoder.transcode()."""

    def test_success_writes_mp4(self, logger, tmp_path):
        """A successful run writes the mp4 and removes the .part file."""
        avi = str(tmp_path / "v.avi")
        mp4 = str(tmp_path / "v.mp4")
        open(avi, "wb").write(b"avi")
        seen = {}

        def runner(cmd, _to, _cb=None):
            seen["cmd"] = cmd
            with open(cmd[-1], "wb") as fh:
                fh.write(b"mp4")
            return 0, ""

        t = TimelapseTranscoder(
            logger, ffmpeg_path="/usr/bin/ffmpeg", runner=runner
        )
        t.transcode(avi, mp4)
        assert os.path.exists(mp4)
        assert not os.path.exists(mp4 + ".part")
        cmd = seen["cmd"]
        assert cmd[-1] == mp4 + ".part"
        assert "-f" in cmd and cmd[cmd.index("-f") + 1] == "mp4"

    def test_threads_zero_preserved(self, logger, tmp_path):
        """threads=0 (all cores) reaches ffmpeg as -threads 0, not 1.

        Regression: ``threads or 1`` capped the intended 0 to a single core.
        """
        avi = str(tmp_path / "v.avi")
        mp4 = str(tmp_path / "v.mp4")
        open(avi, "wb").write(b"avi")
        seen = {}

        def runner(cmd, _to, _cb=None):
            seen["cmd"] = cmd
            with open(cmd[-1], "wb") as fh:
                fh.write(b"mp4")
            return 0, ""

        t = TimelapseTranscoder(
            logger, ffmpeg_path="/usr/bin/ffmpeg", threads=0, runner=runner
        )
        t.transcode(avi, mp4)
        cmd = seen["cmd"]
        assert cmd[cmd.index("-threads") + 1] == "0"

    def test_no_ffmpeg_raises(self, logger):
        """transcode() raises no_ffmpeg when ffmpeg is not configured."""
        t = TimelapseTranscoder(logger, ffmpeg_path=None)
        with pytest.raises(TranscodeError) as exc:
            t.transcode("x.avi", "x.mp4")
        assert exc.value.reason == "no_ffmpeg"

    def test_ffmpeg_failure_cleans_part(self, logger, tmp_path):
        """A non-zero ffmpeg exit raises and removes the .part file."""
        avi = str(tmp_path / "v.avi")
        mp4 = str(tmp_path / "v.mp4")
        open(avi, "wb").write(b"avi")
        t = TimelapseTranscoder(
            logger,
            ffmpeg_path="/usr/bin/ffmpeg",
            runner=lambda cmd, to, cb=None: (1, "boom\nfatal error"),
        )
        with pytest.raises(TranscodeError) as exc:
            t.transcode(avi, mp4)
        assert exc.value.reason == "ffmpeg_failed"
        assert not os.path.exists(mp4)
        assert not os.path.exists(mp4 + ".part")

    def test_empty_output_raises(self, logger, tmp_path):
        """An empty output file raises empty_output."""
        avi = str(tmp_path / "v.avi")
        mp4 = str(tmp_path / "v.mp4")
        open(avi, "wb").write(b"avi")
        t = TimelapseTranscoder(
            logger,
            ffmpeg_path="/usr/bin/ffmpeg",
            runner=_writing_runner(out_bytes=b""),
        )
        with pytest.raises(TranscodeError) as exc:
            t.transcode(avi, mp4)
        assert exc.value.reason == "empty_output"


class TestThumbnail:
    """Tests for TimelapseTranscoder.create_thumbnail()."""

    def test_thumbnail_created_from_last_frame(self, logger, tmp_path):
        """create_thumbnail() seeks to the end (-sseof) and writes a thumb.

        No OctoPrint ``ffmpegThumbnailCommandline`` is configured — the method
        must build its own command and still succeed (regression: relying on
        that template produced no thumbnail on recent OctoPrint).
        """
        mp4 = str(tmp_path / "v.mp4")
        thumb = mp4 + ".thumb.jpg"
        open(mp4, "wb").write(b"mp4")
        seen = {}

        def runner(cmd, _to, _cb=None):
            seen["cmd"] = cmd
            with open(cmd[-1], "wb") as fh:
                fh.write(b"jpg")
            return 0, ""

        t = TimelapseTranscoder(
            logger, ffmpeg_path="/usr/bin/ffmpeg", runner=runner
        )
        assert t.create_thumbnail(mp4, thumb) is True
        assert os.path.exists(thumb)
        assert "-sseof" in seen["cmd"]
        assert seen["cmd"][seen["cmd"].index("-sseof") + 1] == "-1"

    def test_thumbnail_no_ffmpeg_returns_false(self, logger):
        """create_thumbnail() returns False when ffmpeg is not configured."""
        t = TimelapseTranscoder(logger, ffmpeg_path=None)
        assert t.create_thumbnail("v.mp4", "v.thumb.jpg") is False

    def test_thumbnail_falls_back_to_start_frame(self, logger, tmp_path):
        """An end-seek that fails is retried from the start (frame 0)."""
        mp4 = str(tmp_path / "v.mp4")
        open(mp4, "wb").write(b"mp4")
        thumb = mp4 + ".thumb.jpg"
        calls = []

        def runner(cmd, _to, _cb=None):
            calls.append(cmd)
            if "-sseof" in cmd:
                return 1, "seek fail"  # end-seek fails
            with open(cmd[-1], "wb") as fh:
                fh.write(b"jpg")
            return 0, ""

        t = TimelapseTranscoder(
            logger, ffmpeg_path="/usr/bin/ffmpeg", runner=runner
        )
        assert t.create_thumbnail(mp4, thumb) is True
        assert len(calls) == 2  # end-seek, then start-frame retry
        assert "-sseof" not in calls[1]

    def test_thumbnail_failure_non_fatal(self, logger, tmp_path):
        """Both attempts failing returns False without raising."""
        mp4 = str(tmp_path / "v.mp4")
        open(mp4, "wb").write(b"mp4")
        t = TimelapseTranscoder(
            logger,
            ffmpeg_path="/usr/bin/ffmpeg",
            runner=lambda cmd, to, cb=None: (1, "fail"),
        )
        assert t.create_thumbnail(mp4, mp4 + ".thumb.jpg") is False


def _fake_stream(text):
    """A char-at-a-time .read(1) stream over ``text`` (fake ffmpeg stderr)."""

    class _S:
        def __init__(self):
            self._it = iter(text)

        def read(self, _n):
            """Return the next char of the prepared text, or '' at EOF."""
            return next(self._it, "")

    return _S()


class TestGcodeThumbnail:
    """Tests for TimelapseTranscoder.create_gcode_thumbnail()."""

    def test_scales_preview_to_thumb(self, logger, tmp_path):
        """A plate preview is scaled down and written to thumb_path."""
        src = str(tmp_path / "plate_1.png")
        thumb = str(tmp_path / "v.mp4.thumb.jpg")
        open(src, "wb").write(b"png")
        seen = {}

        def runner(cmd, _to, _cb=None):
            seen["cmd"] = cmd
            open(cmd[-1], "wb").write(b"jpg")
            return 0, ""

        t = TimelapseTranscoder(
            logger, ffmpeg_path="/usr/bin/ffmpeg", runner=runner
        )
        assert t.create_gcode_thumbnail(src, thumb) is True
        assert os.path.isfile(thumb)
        assert src in seen["cmd"]
        assert "scale=640:-1" in seen["cmd"]

    def test_empty_source_returns_false(self, logger, tmp_path):
        """No preview PNG -> False without running ffmpeg."""
        t = TimelapseTranscoder(
            logger,
            ffmpeg_path="/usr/bin/ffmpeg",
            runner=_writing_runner(),
        )
        assert t.create_gcode_thumbnail("", str(tmp_path / "t.jpg")) is False

    def test_unavailable_returns_false(self, logger, tmp_path):
        """Without a configured ffmpeg the helper declines."""
        t = TimelapseTranscoder(logger, ffmpeg_path=None)
        assert (
            t.create_gcode_thumbnail("plate.png", str(tmp_path / "t.jpg"))
            is False
        )

    def test_ffmpeg_failure_returns_false(self, logger, tmp_path):
        """A non-zero ffmpeg exit is reported as False (fallback kicks in)."""
        t = TimelapseTranscoder(
            logger,
            ffmpeg_path="/usr/bin/ffmpeg",
            runner=_writing_runner(rc=1, err="boom"),
        )
        assert (
            t.create_gcode_thumbnail("plate.png", str(tmp_path / "t.jpg"))
            is False
        )

    def test_runner_exception_returns_false(self, logger, tmp_path):
        """An OSError from the runner is swallowed (best-effort thumb)."""

        def boom(_cmd, _to, _cb=None):
            raise OSError("no exec")

        t = TimelapseTranscoder(
            logger, ffmpeg_path="/usr/bin/ffmpeg", runner=boom
        )
        assert (
            t.create_gcode_thumbnail("plate.png", str(tmp_path / "t.jpg"))
            is False
        )


class TestTranscodeOsError:
    """transcode() wraps filesystem errors as TranscodeError(io_error)."""

    def test_replace_failure_raises_io_error(
        self, logger, tmp_path, monkeypatch
    ):
        """An OSError moving the finished tmp file cleans up and re-raises."""
        avi = str(tmp_path / "v.avi")
        mp4 = str(tmp_path / "v.mp4")
        open(avi, "wb").write(b"avi")

        def boom_replace(_src, _dst):
            raise OSError("disk gone")

        monkeypatch.setattr(tc.os, "replace", boom_replace)
        t = TimelapseTranscoder(
            logger,
            ffmpeg_path="/usr/bin/ffmpeg",
            runner=_writing_runner(),
        )
        with pytest.raises(TranscodeError) as exc:
            t.transcode(avi, mp4)
        assert exc.value.reason == "io_error"
        assert not os.path.exists(mp4 + ".part")


class TestProgressParsing:
    """Tests for ffmpeg stderr progress parsing in _run_command."""

    def test_runner_emits_percent_from_stderr(self, monkeypatch):
        """_run_command parses Duration: + time= into percent callbacks."""
        text = (
            "  Duration: 00:00:10.00, start: 0.0\r"
            "frame=1 time=00:00:00.00\r"
            "frame=2 time=00:00:05.00\r"
            "frame=3 time=00:00:10.00\r"
        )

        class FakeProc:
            """Fake Popen emitting the prepared stderr and exiting cleanly."""

            stderr = _fake_stream(text)
            returncode = 0

            def wait(self):
                """Return the process exit code."""
                return 0

            def kill(self):
                """No-op kill (process already finished)."""

        monkeypatch.setattr(tc.subprocess, "Popen", lambda *a, **k: FakeProc())
        seen = []
        rc, _tail = tc._run_command(["ffmpeg"], 1800, seen.append)
        assert rc == 0
        assert seen == [0, 50, 100]

    def test_iter_splits_cr_and_lf(self):
        """_iter_ffmpeg_lines splits on both CR and LF."""
        assert list(_iter_ffmpeg_lines(_fake_stream("a\rb\nc"))) == [
            "a",
            "b",
            "c",
        ]

    def test_iter_flushes_runaway_line(self):
        """_iter_ffmpeg_lines flushes a line that never terminates."""
        out = list(_iter_ffmpeg_lines(_fake_stream("x" * 2500)))
        assert all(len(s) <= 1024 for s in out)
        assert "".join(out) == "x" * 2500

    def test_runner_oserror_raises(self, monkeypatch):
        """An OSError launching ffmpeg raises ffmpeg_failed."""

        def boom(*a, **k):
            raise OSError("no ffmpeg binary")

        monkeypatch.setattr(tc.subprocess, "Popen", boom)
        with pytest.raises(TranscodeError) as exc:
            tc._run_command(["ffmpeg"], 1800)
        assert exc.value.reason == "ffmpeg_failed"

    def test_runner_cancel_kills(self, monkeypatch):
        """A set cancel event kills the process and returns promptly.

        Regression: the render queue's Cancel button set the event but the
        long ffmpeg encode ran to completion because the event was never
        polled inside the runner.
        """
        import threading

        class FakeProc:
            """Fake Popen streaming forever until killed."""

            stderr = _fake_stream("frame=1 time=00:00:01.00\r" * 1000)
            returncode = -9

            def __init__(self):
                self.killed = False

            def kill(self):
                """Record that the process was killed."""
                self.killed = True

            def wait(self):
                """Return the process exit code."""
                return self.returncode

        proc = FakeProc()
        monkeypatch.setattr(tc.subprocess, "Popen", lambda *a, **k: proc)
        cancel = threading.Event()
        cancel.set()  # already cancelled → killed on the first stderr line
        rc, _tail = tc._run_command(["ffmpeg"], 1800, None, cancel)
        assert proc.killed
        assert rc == -9

    def test_runner_timeout_kills(self, monkeypatch):
        """Exceeding the timeout kills the process and raises timeout."""

        class FakeProc:
            """Fake Popen that records whether it was killed."""

            stderr = _fake_stream("frame=1 time=00:00:01.00\r" * 1000)
            returncode = 0

            def __init__(self):
                self.killed = False

            def kill(self):
                """Record that the process was killed."""
                self.killed = True

            def wait(self):
                """Return the process exit code."""
                return 0

        proc = FakeProc()
        monkeypatch.setattr(tc.subprocess, "Popen", lambda *a, **k: proc)

        clock = iter([0.0] + [10**9] * 100)
        monkeypatch.setattr(tc.time, "monotonic", lambda: next(clock))
        with pytest.raises(TranscodeError) as exc:
            tc._run_command(["ffmpeg"], 1)
        assert exc.value.reason == "timeout"
        assert proc.killed
