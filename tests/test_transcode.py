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

    def test_thumbnail_created(self, logger, tmp_path):
        """create_thumbnail() writes a thumbnail and returns True."""
        mp4 = str(tmp_path / "v.mp4")
        thumb = mp4 + ".thumb.jpg"
        open(mp4, "wb").write(b"mp4")
        t = TimelapseTranscoder(
            logger,
            ffmpeg_path="/usr/bin/ffmpeg",
            thumbnail_commandline='{ffmpeg} -i "{input}" "{output}"',
            runner=_writing_runner(),
        )
        assert t.create_thumbnail(mp4, thumb) is True
        assert os.path.exists(thumb)

    def test_thumbnail_no_template_returns_false(self, logger):
        """create_thumbnail() returns False without a command template."""
        t = TimelapseTranscoder(logger, ffmpeg_path="/usr/bin/ffmpeg")
        assert t.create_thumbnail("v.mp4", "v.thumb.jpg") is False

    def test_thumbnail_failure_non_fatal(self, logger, tmp_path):
        """A failing thumbnail run returns False without raising."""
        mp4 = str(tmp_path / "v.mp4")
        t = TimelapseTranscoder(
            logger,
            ffmpeg_path="/usr/bin/ffmpeg",
            thumbnail_commandline='{ffmpeg} -i "{input}" "{output}"',
            runner=lambda cmd, to, cb=None: (1, "fail"),
        )
        assert t.create_thumbnail(mp4, mp4 + ".thumb.jpg") is False


def _fake_stream(text):
    """A char-at-a-time .read(1) stream over ``text`` (fake ffmpeg stderr)."""

    class _S:
        def __init__(self):
            self._it = iter(text)

        def read(self, _n):
            return next(self._it, "")

    return _S()


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
                pass

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
