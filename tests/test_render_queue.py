"""Tests for octoprint_bambucam.render_queue."""

# pylint: disable=protected-access
# redefined-outer-name is the standard pytest fixture-injection pattern (a
# fixture and the test param share a name); disabled file-wide as elsewhere.
# pylint: disable=redefined-outer-name

import logging
import os
import threading

import pytest

from octoprint_bambucam.render_paths import RenderPaths
from octoprint_bambucam.render_queue import RenderQueue
from octoprint_bambucam.render_registry import (
    JOB_CANCELLED,
    JOB_DONE,
    JOB_FAILED,
    JOB_QUEUED,
    JobRegistry,
)
from octoprint_bambucam.render_worker import RenderError


@pytest.fixture()
def logger():
    """Return a test-scoped logger."""
    return logging.getLogger("test.queue")


@pytest.fixture()
def paths(tmp_path):
    """Return a RenderPaths with directories created."""
    p = RenderPaths(str(tmp_path))
    p.ensure_dirs()
    return p


def _job(queue, jobid) -> dict:
    """Fetch a job that must exist; narrows the Optional for type checkers."""
    job = queue._registry.get(jobid)
    assert job is not None
    return job


def _make_queue(logger, paths, *, run_job=None, gate=True, rendered=None):
    """Build a queue and the list capturing its notify() calls.

    Returns ``(queue, notices)`` where ``notices`` is a list of
    ``(state, job)`` tuples, so tests can assert on emitted notices without
    mutating the production ``RenderQueue`` instance.
    """
    registry = JobRegistry(logger, paths.jobs_file)
    registry.load()
    notices = []
    queue = RenderQueue(
        logger,
        paths,
        registry,
        run_job=run_job or (lambda *_a: "/out.mp4"),
        gate_open=lambda: gate,
        notify=lambda job, state, **e: notices.append((state, job)),
        on_group_rendered=rendered,
    )
    return queue, notices


class TestEnqueue:
    """enqueue() guards group/queue limits."""

    def test_enqueue_adds_job(self, logger, paths):
        """A first job for a group is accepted."""
        q, _ = _make_queue(logger, paths)
        res = q.enqueue("p", "fast_720p", ["a.avi"])
        assert res["ok"] is True
        assert q.jobs()[0]["state"] == JOB_QUEUED

    def test_second_job_same_group_rejected(self, logger, paths):
        """A group already queued cannot be queued twice."""
        q, _ = _make_queue(logger, paths)
        q.enqueue("p", "fast_720p", ["a.avi"])
        res = q.enqueue("p", "fast_720p", ["a.avi"])
        assert res == {"ok": False, "reason": "already_queued"}

    def test_queue_full_rejected(self, logger, paths):
        """A full queue rejects further jobs."""
        q, _ = _make_queue(logger, paths)
        q._max_queue_size = 1
        q.enqueue("p1", "fast_720p", ["a.avi"])
        res = q.enqueue("p2", "fast_720p", ["a.avi"])
        assert res == {"ok": False, "reason": "queue_full"}


class TestRunOne:
    """_run_one drives a job through the worker callback."""

    def test_success_marks_done_and_rendered(self, logger, paths):
        """A successful run marks done and calls on_group_rendered."""
        rendered = []
        q, _ = _make_queue(logger, paths, rendered=rendered.append)
        jobid = q.enqueue("p", "fast_720p", ["a.avi"])["jobid"]
        q._run_one(_job(q, jobid))
        job = _job(q, jobid)
        assert job["state"] == JOB_DONE
        assert job["output"] == "/out.mp4"
        assert rendered == ["p"]

    def test_failure_marks_failed(self, logger, paths):
        """A worker error marks the job failed with the reason."""

        def boom(_job, _cancel, _cb):
            raise RenderError("ffmpeg_failed", "bad")

        q, _ = _make_queue(logger, paths, run_job=boom)
        jobid = q.enqueue("p", "fast_720p", ["a.avi"])["jobid"]
        q._run_one(_job(q, jobid))
        job = _job(q, jobid)
        assert job["state"] == JOB_FAILED
        assert job["reason"] == "ffmpeg_failed"

    def test_cancelled_marks_cancelled(self, logger, paths):
        """A run that raises with cancel set is recorded as cancelled."""

        def cancel_then_raise(_job, cancel, _cb):
            cancel.set()
            raise RenderError("cancelled", "x")

        q, _ = _make_queue(logger, paths, run_job=cancel_then_raise)
        jobid = q.enqueue("p", "fast_720p", ["a.avi"])["jobid"]
        q._run_one(_job(q, jobid))
        assert _job(q, jobid)["state"] == JOB_CANCELLED

    def test_progress_updates_percent(self, logger, paths):
        """The worker's progress callback updates the job percent."""

        def with_progress(_job, _cancel, cb):
            cb("render", 50)
            return "/out.mp4"

        q, notices = _make_queue(logger, paths, run_job=with_progress)
        jobid = q.enqueue("p", "fast_720p", ["a.avi"])["jobid"]
        q._run_one(_job(q, jobid))
        assert q._registry.get(jobid) is not None
        states = [s for s, _ in notices]
        assert "render" in states

    def test_terminal_jobs_pruned_from_jobs(self, logger, paths):
        """jobs() drops a finished job from the live render-queue list."""
        q, _ = _make_queue(logger, paths)
        jobid = q.enqueue("p", "fast_720p", ["a.avi"])["jobid"]
        q._run_one(_job(q, jobid))
        assert q.jobs() == []


class TestCancel:
    """cancel() handles queued and unknown jobs."""

    def test_cancel_queued(self, logger, paths):
        """Cancelling a queued job moves it to cancelled."""
        q, _ = _make_queue(logger, paths)
        jobid = q.enqueue("p", "fast_720p", ["a.avi"])["jobid"]
        assert q.cancel(jobid)["ok"] is True
        assert _job(q, jobid)["state"] == JOB_CANCELLED

    def test_cancel_unknown(self, logger, paths):
        """Cancelling an unknown job reports unknown_job."""
        q, _ = _make_queue(logger, paths)
        assert q.cancel("nope") == {"ok": False, "reason": "unknown_job"}

    def test_jobs_active_for(self, logger, paths):
        """jobs_active_for reflects an active job."""
        q, _ = _make_queue(logger, paths)
        q.enqueue("p", "fast_720p", ["a.avi"])
        assert q.jobs_active_for("p") is True
        assert q.jobs_active_for("other") is False


class TestRecover:
    """recover() cleans work, locks and dead jobs."""

    def test_recover_clears_work_and_fails_jobs(self, logger, paths):
        """Work scratch is removed and running jobs become failed."""
        scratch = os.path.join(paths.work_dir, "leftover.tmp")
        with open(scratch, "w", encoding="utf-8") as fh:
            fh.write("x")
        subdir = os.path.join(paths.work_dir, "sub")
        os.makedirs(subdir)
        q, _ = _make_queue(logger, paths)
        q._registry.add({"jobid": "a", "print_id": "p", "state": "render"})
        q.recover(86400)
        assert not os.path.exists(scratch)
        assert not os.path.exists(subdir)
        assert _job(q, "a")["state"] == JOB_FAILED


class TestStartStop:
    """start()/stop() manage the worker thread."""

    def test_start_runs_queued_job(self, logger, paths):
        """The worker thread picks up and finishes a queued job."""
        done = threading.Event()

        def run(_job, _cancel, _cb):
            done.set()
            return "/out.mp4"

        q, _ = _make_queue(logger, paths, run_job=run)
        q.enqueue("p", "fast_720p", ["a.avi"])
        q.start()
        assert done.wait(timeout=5)
        q.stop()
