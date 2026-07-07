"""Tests for octoprint_bambucam.render_registry."""

# pylint: disable=protected-access
# redefined-outer-name is the standard pytest fixture-injection pattern (a
# fixture and the test param share a name); disabled file-wide as elsewhere.
# pylint: disable=redefined-outer-name

import json
import logging
import os
import time

import pytest

from octoprint_bambucam.render_registry import (
    JOB_CONCAT,
    JOB_QUEUED,
    JobRegistry,
    LockError,
    Lockfile,
    clear_stale_locks,
)


@pytest.fixture()
def logger():
    """Return a test-scoped logger."""
    return logging.getLogger("test.registry")


@pytest.fixture()
def registry(logger, tmp_path):
    """Return a loaded JobRegistry over a temp jobs.json."""
    reg = JobRegistry(logger, str(tmp_path / "meta" / "jobs.json"))
    reg.load()
    return reg


def _job(registry, jobid) -> dict:
    """Fetch a job that must exist; narrows the Optional for type checkers."""
    job = registry.get(jobid)
    assert job is not None
    return job


class TestJobRegistry:
    """add/update/remove/query + atomic persistence."""

    def test_add_persists(self, registry):
        """An added job is queryable and written to disk."""
        registry.add({"jobid": "a", "print_id": "p", "state": JOB_QUEUED})
        assert _job(registry, "a")["state"] == JOB_QUEUED
        with open(registry._jobs_file, encoding="utf-8") as fh:
            assert "a" in json.load(fh)

    def test_update_and_remove(self, registry):
        """update merges fields; remove drops the job."""
        registry.add({"jobid": "a", "print_id": "p", "state": JOB_QUEUED})
        registry.update("a", state=JOB_CONCAT, percent=10)
        assert _job(registry, "a")["state"] == JOB_CONCAT
        assert registry.update("missing", x=1) is None
        registry.remove("a")
        assert registry.get("a") is None

    def test_active_for_group(self, registry):
        """active_for_group finds a queued/running job by print id."""
        registry.add({"jobid": "a", "print_id": "p", "state": JOB_QUEUED})
        active = registry.active_for_group("p")
        assert active is not None
        assert active["jobid"] == "a"
        assert registry.active_for_group("other") is None

    def test_reconcile_active_fails_running(self, registry):
        """reconcile_active flips running jobs to failed/interrupted."""
        registry.add({"jobid": "a", "print_id": "p", "state": JOB_CONCAT})
        changed = registry.reconcile_active()
        assert changed == ["a"]
        job = registry.get("a")
        assert job["state"] == "failed"
        assert job["reason"] == "interrupted"

    def test_load_corrupt_is_empty(self, logger, tmp_path):
        """A corrupt jobs.json loads as an empty registry."""
        jf = tmp_path / "jobs.json"
        jf.write_text("not json")
        reg = JobRegistry(logger, str(jf))
        reg.load()
        assert reg.all() == []


class TestLockfile:
    """O_EXCL lockfile acquire/release/reclaim."""

    def test_acquire_and_release(self, tmp_path):
        """A lock is created then removed."""
        path = str(tmp_path / "x.lock")
        lock = Lockfile(path)
        lock.acquire()
        assert os.path.exists(path)
        lock.release()
        assert not os.path.exists(path)

    def test_second_acquire_blocks(self, tmp_path):
        """A held, fresh lock rejects a second acquirer."""
        path = str(tmp_path / "x.lock")
        with Lockfile(path):
            with pytest.raises(LockError):
                Lockfile(path).acquire()

    def test_stale_lock_reclaimed(self, tmp_path):
        """A lock older than stale_after is reclaimed."""
        path = str(tmp_path / "x.lock")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("999 0")
        old = time.time() - 100
        os.utime(path, (old, old))
        lock = Lockfile(path, stale_after=10)
        lock.acquire()  # must not raise
        assert lock._held
        lock.release()


class TestClearStaleLocks:
    """clear_stale_locks sweeps old *.lock files."""

    def test_removes_old_keeps_new(self, tmp_path):
        """Only locks older than the timeout are removed."""
        old = tmp_path / "old.lock"
        new = tmp_path / "new.lock"
        old.write_text("x")
        new.write_text("x")
        past = time.time() - 100
        os.utime(str(old), (past, past))
        removed = clear_stale_locks(str(tmp_path), 10)
        assert removed == 1
        assert not old.exists()
        assert new.exists()

    def test_missing_dir_is_zero(self, tmp_path):
        """A missing directory removes nothing."""
        assert clear_stale_locks(str(tmp_path / "nope"), 10) == 0
