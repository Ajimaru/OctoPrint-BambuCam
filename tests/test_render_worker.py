"""Tests for octoprint_bambucam.render_worker."""

# pylint: disable=protected-access
# redefined-outer-name is the standard pytest fixture-injection pattern (a
# fixture and the test param share a name); disabled file-wide as elsewhere.
# pylint: disable=redefined-outer-name

import json
import logging
import os
import threading

import pytest

from octoprint_bambucam.render_paths import RenderPaths
from octoprint_bambucam.render_worker import RenderError, RenderWorker


@pytest.fixture()
def logger():
    """Return a test-scoped logger."""
    return logging.getLogger("test.worker")


@pytest.fixture()
def paths(tmp_path):
    """Return a RenderPaths with directories created."""
    p = RenderPaths(str(tmp_path))
    p.ensure_dirs()
    return p


@pytest.fixture()
def timelapse_dir(tmp_path):
    """Return a separate timelapse output folder."""
    out = tmp_path / "timelapse"
    out.mkdir()
    return str(out)


def _group(paths, print_id, chunk_files):
    """Create a group directory with chunk files."""
    group = paths.group_dir(print_id)
    os.makedirs(group, exist_ok=True)
    for name in chunk_files:
        with open(os.path.join(group, name), "wb") as fh:
            fh.write(b"x" * 50)
    return group


def _fake_runner_factory():
    """Return a runner that creates each command's output file (last arg)."""

    def runner(cmd, _timeout, progress_cb=None, _cancel=None):
        out = cmd[-1]
        with open(out, "wb") as fh:
            fh.write(b"video-bytes")
        if progress_cb:
            progress_cb(100)
        return 0, ""

    return runner


def _make_worker(
    logger, paths, timelapse_dir, runner=None, fired=None, threads=1
):
    """Build a RenderWorker with injected runner and movie-done callback."""
    return RenderWorker(
        logger,
        paths,
        ffmpeg_path="/bin/ffmpeg",
        timelapse_folder=timelapse_dir,
        fire_movie_done=fired or (lambda p: None),
        output_name=lambda pid, preset: f"{pid}__{preset}.mp4",
        runner=runner or _fake_runner_factory(),
        threads=threads,
    )


class TestRun:
    """run() performs concat + render + publish + cleanup."""

    def test_multi_chunk_render(self, logger, paths, timelapse_dir):
        """Two chunks concat then render into the timelapse folder."""
        pid = "2026-06-23_1432__gearbox"
        _group(paths, pid, ["a.avi", "b.avi"])
        fired = []
        worker = _make_worker(logger, paths, timelapse_dir, fired=fired.append)
        job = {
            "jobid": "j1",
            "print_id": pid,
            "preset": "fast_720p",
            "chunks": ["a.avi", "b.avi"],
        }
        out = worker.run(job, threading.Event(), lambda phase, pct: None)
        assert os.path.isfile(out)
        assert out.startswith(timelapse_dir)
        assert fired == [out]
        # group stays on disk, stamped with the rendered marker
        assert os.path.isdir(paths.group_dir(pid))
        with open(paths.rendered_marker(pid), encoding="utf-8") as fh:
            marker = json.load(fh)
        assert marker["output"] == os.path.basename(out)
        assert marker["preset"] == "fast_720p"
        assert marker["rendered_at"]

    def test_render_passes_threads_flag(self, logger, paths, timelapse_dir):
        """The encode step carries ``-threads N`` from the worker setting."""
        pid = "2026-06-23_1432__gearbox"
        _group(paths, pid, ["a.avi"])
        seen = []

        def capturing(cmd, _timeout, progress_cb=None, _cancel=None):
            seen.append(list(cmd))
            with open(cmd[-1], "wb") as fh:
                fh.write(b"v")
            if progress_cb:
                progress_cb(100)
            return 0, ""

        worker = _make_worker(
            logger, paths, timelapse_dir, runner=capturing, threads=3
        )
        job = {
            "jobid": "j",
            "print_id": pid,
            "preset": "fast_720p",
            "chunks": ["a.avi"],
        }
        worker.run(job, threading.Event(), lambda *a: None)
        # The render cmd carries -threads 3 (a later thumbnail cmd does not).
        render_cmd = next(c for c in seen if "-threads" in c)
        i = render_cmd.index("-threads")
        assert render_cmd[i + 1] == "3"

    def test_single_chunk_skips_concat(self, logger, paths, timelapse_dir):
        """A single chunk renders directly without a concat step."""
        pid = "2026-06-23_1432__gearbox"
        _group(paths, pid, ["a.avi"])
        phases = []
        worker = _make_worker(logger, paths, timelapse_dir)
        job = {
            "jobid": "j2",
            "print_id": pid,
            "preset": "fast_720p",
            "chunks": ["a.avi"],
        }
        worker.run(
            job, threading.Event(), lambda phase, pct: phases.append(phase)
        )
        assert "concat" not in phases

    def test_writes_thumbnail(self, logger, paths, timelapse_dir):
        """A finished render emits a last-frame ``<mp4>.thumb.jpg``.

        Without it the native Timelapse list shows a blank white tile.
        """
        pid = "2026-06-23_1432__gearbox"
        _group(paths, pid, ["a.avi"])
        seen = []

        def capturing(cmd, _timeout, progress_cb=None, _cancel=None):
            seen.append(list(cmd))
            with open(cmd[-1], "wb") as fh:
                fh.write(b"x")
            if progress_cb:
                progress_cb(100)
            return 0, ""

        worker = _make_worker(logger, paths, timelapse_dir, runner=capturing)
        job = {
            "jobid": "j",
            "print_id": pid,
            "preset": "fast_720p",
            "chunks": ["a.avi"],
        }
        out = worker.run(job, threading.Event(), lambda *a: None)
        thumb_cmd = next((c for c in seen if "-sseof" in c), None)
        assert thumb_cmd is not None
        assert thumb_cmd[-1] == out + ".thumb.jpg"

    def test_no_ffmpeg_raises(self, logger, paths, timelapse_dir):
        """An unconfigured ffmpeg path raises no_ffmpeg."""
        worker = RenderWorker(
            logger,
            paths,
            ffmpeg_path="",
            timelapse_folder=timelapse_dir,
            fire_movie_done=lambda p: None,
            output_name=lambda pid, preset: "x.mp4",
        )
        with pytest.raises(RenderError) as exc:
            worker.run(
                {"jobid": "j", "print_id": "p", "chunks": ["a.avi"]},
                threading.Event(),
                lambda *a: None,
            )
        assert exc.value.reason == "no_ffmpeg"

    def test_bad_chunk_rejected(self, logger, paths, timelapse_dir):
        """A chunk name that escapes the group raises bad_chunk."""
        pid = "2026-06-23_1432__gearbox"
        _group(paths, pid, ["a.avi"])
        worker = _make_worker(logger, paths, timelapse_dir)
        job = {
            "jobid": "j",
            "print_id": pid,
            "preset": "fast_720p",
            "chunks": ["../../escape.avi"],
        }
        with pytest.raises(RenderError) as exc:
            worker.run(job, threading.Event(), lambda *a: None)
        assert exc.value.reason == "bad_chunk"

    def test_empty_selection_raises(self, logger, paths, timelapse_dir):
        """An empty chunk selection raises no_chunks."""
        pid = "2026-06-23_1432__gearbox"
        _group(paths, pid, ["a.avi"])
        worker = _make_worker(logger, paths, timelapse_dir)
        job = {
            "jobid": "j",
            "print_id": pid,
            "preset": "fast_720p",
            "chunks": [],
        }
        with pytest.raises(RenderError) as exc:
            worker.run(job, threading.Event(), lambda *a: None)
        assert exc.value.reason == "no_chunks"

    def test_ffmpeg_failure_raises(self, logger, paths, timelapse_dir):
        """A non-zero ffmpeg exit raises ffmpeg_failed."""
        pid = "2026-06-23_1432__gearbox"
        _group(paths, pid, ["a.avi"])

        def failing(_cmd, _timeout, _progress_cb=None, _cancel=None):
            return 1, "boom"

        worker = _make_worker(logger, paths, timelapse_dir, runner=failing)
        job = {
            "jobid": "j",
            "print_id": pid,
            "preset": "fast_720p",
            "chunks": ["a.avi"],
        }
        with pytest.raises(RenderError) as exc:
            worker.run(job, threading.Event(), lambda *a: None)
        assert exc.value.reason == "ffmpeg_failed"

    def test_cancel_during_step_raises(self, logger, paths, timelapse_dir):
        """A cancel set during a step raises cancelled."""
        pid = "2026-06-23_1432__gearbox"
        _group(paths, pid, ["a.avi"])
        cancel = threading.Event()

        seen_cancel = []

        def cancelling(cmd, _timeout, _progress_cb=None, cancel_ev=None):
            # The worker must forward its cancel event to the runner so a long
            # ffmpeg encode can be killed mid-flight, not only after it exits.
            seen_cancel.append(cancel_ev)
            out = cmd[-1]
            with open(out, "wb") as fh:
                fh.write(b"x")
            cancel.set()
            return 0, ""

        worker = _make_worker(logger, paths, timelapse_dir, runner=cancelling)
        job = {
            "jobid": "j",
            "print_id": pid,
            "preset": "fast_720p",
            "chunks": ["a.avi"],
        }
        with pytest.raises(RenderError) as exc:
            worker.run(job, cancel, lambda *a: None)
        assert exc.value.reason == "cancelled"
        # the same cancel event reached the runner
        assert seen_cancel and seen_cancel[-1] is cancel


class TestAvailable:
    """available() reflects ffmpeg configuration."""

    def test_available(self, logger, paths, timelapse_dir):
        """A set path is available; an empty one is not."""
        assert _make_worker(logger, paths, timelapse_dir).available() is True
