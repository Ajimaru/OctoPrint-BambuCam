"""Atomic job registry and cross-process lockfiles for the render pipeline.

Implements the TimelapsePlus safety rules (plan §3.5/§6): a single lock-guarded
map of render jobs persisted atomically to ``metadata/jobs.json`` (tmp +
``os.replace`` + ``fsync``), plus advisory ``*.lock`` files created with
``O_CREAT|O_EXCL`` so a second process (ffmpeg, cleanup, an external watcher)
cannot grab a group already owned by a running job.

This module owns *job* state (``queued``/``concat``/``render``/``done``/
``failed``/``cancelled``); the *group* state (``incomplete``/``chunks_ready``)
lives in :mod:`raw_library`.
"""

import json
import logging
import os
import threading
import time
from typing import Optional

JOB_QUEUED = "queued"
JOB_CONCAT = "concat"
JOB_RENDER = "render"
JOB_DONE = "done"
JOB_FAILED = "failed"
JOB_CANCELLED = "cancelled"

ACTIVE_JOB_STATES = (JOB_QUEUED, JOB_CONCAT, JOB_RENDER)


class LockError(Exception):
    """A lockfile is already held by another owner."""


class JobRegistry:
    """Lock-guarded, atomically-persisted map of render jobs.

    Construct with the ``jobs.json`` path. All mutations take the internal lock
    and persist immediately so a crash never leaves the on-disk state behind
    the in-memory state by more than one operation.
    """

    def __init__(self, logger: logging.Logger, jobs_file: str):
        self._logger = logger
        self._jobs_file = jobs_file
        self._lock = threading.Lock()
        self._jobs: dict = {}

    def load(self) -> None:
        """Load ``jobs.json`` into memory (missing/corrupt → empty)."""
        with self._lock:
            self._jobs = self._read_file()

    def add(self, job: dict) -> None:
        """Insert a job (keyed by ``jobid``) and persist."""
        with self._lock:
            self._jobs[job["jobid"]] = job
            self._persist()

    def update(self, jobid: str, **fields) -> Optional[dict]:
        """Merge ``fields`` into a job and persist; return the job or None."""
        with self._lock:
            job = self._jobs.get(jobid)
            if job is None:
                return None
            job.update(fields)
            self._persist()
            return dict(job)

    def remove(self, jobid: str) -> None:
        """Drop a job and persist."""
        with self._lock:
            if self._jobs.pop(jobid, None) is not None:
                self._persist()

    def get(self, jobid: str) -> Optional[dict]:
        """Return a copy of one job by id, or ``None`` if unknown."""
        with self._lock:
            job = self._jobs.get(jobid)
            return dict(job) if job else None

    def all(self) -> list:
        """Return copies of every job (insertion order)."""
        with self._lock:
            return [dict(j) for j in self._jobs.values()]

    def active_for_group(self, print_id: str) -> Optional[dict]:
        """Return an active (queued/running) job for a group, if any."""
        with self._lock:
            for job in self._jobs.values():
                if (
                    job.get("print_id") == print_id
                    and job.get("state") in ACTIVE_JOB_STATES
                ):
                    return dict(job)
        return None

    def reconcile_active(self) -> list:
        """Fail jobs left ``concat``/``render`` by a crash; return their ids.

        Called on startup recovery: a job whose process died mid-render is no
        longer running, so it is moved to ``failed`` with a ``reason`` of
        ``interrupted`` and its group falls back to ``chunks_ready``.
        """
        changed = []
        with self._lock:
            for jobid, job in self._jobs.items():
                if job.get("state") in (JOB_CONCAT, JOB_RENDER, JOB_QUEUED):
                    job["state"] = JOB_FAILED
                    job["reason"] = "interrupted"
                    changed.append(jobid)
            if changed:
                self._persist()
        return changed

    def _read_file(self) -> dict:
        try:
            with open(self._jobs_file, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {k: v for k, v in data.items() if isinstance(v, dict)}

    def _persist(self) -> None:
        """Atomically write the map (tmp + replace + fsync). Caller holds lock.

        A persistence failure is logged but not raised: the in-memory state is
        still authoritative for the running process, and the next mutation
        retries the write.
        """
        tmp = self._jobs_file + ".tmp"
        try:
            os.makedirs(os.path.dirname(self._jobs_file), exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._jobs, fh)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self._jobs_file)
        except OSError:
            self._logger.warning("could not persist jobs.json")
            _silent_remove(tmp)


class Lockfile:
    """Advisory cross-process lock via ``O_CREAT|O_EXCL`` (plan §3.5/§8).

    :meth:`acquire` creates ``<path>`` atomically with the owner's PID and a
    timestamp; a second caller gets :class:`LockError`. A stale lock (older
    than ``stale_after`` seconds) is reclaimed. Use as a context manager.
    """

    def __init__(self, path: str, *, stale_after: int = 86400):
        self._path = path
        self._stale_after = stale_after
        self._held = False

    def __enter__(self) -> "Lockfile":
        self.acquire()
        return self

    def __exit__(self, *_exc) -> None:
        self.release()

    def acquire(self) -> None:
        """Take the lock, reclaiming it first if the holder went stale.

        Raises :class:`LockError` when the lock is held and not yet stale.
        """
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        try:
            fd = os.open(self._path, flags, 0o644)
        except FileExistsError as exc:
            if not self._reclaim_if_stale():
                raise LockError(self._path) from exc
            fd = os.open(self._path, flags, 0o644)
        with os.fdopen(fd, "w") as fh:
            fh.write(f"{os.getpid()} {int(time.time())}")
        self._held = True

    def release(self) -> None:
        """Remove the lock file if this instance holds it (idempotent)."""
        if self._held:
            _silent_remove(self._path)
            self._held = False

    def _reclaim_if_stale(self) -> bool:
        try:
            age = time.time() - os.path.getmtime(self._path)
        except OSError:
            return False
        if age < self._stale_after:
            return False
        _silent_remove(self._path)
        return True


def clear_stale_locks(directory: str, stale_after: int) -> int:
    """Remove ``*.lock`` files older than ``stale_after`` seconds.

    Used by startup recovery and the diagnostics danger-zone. Returns the
    number of locks removed. A missing directory is a no-op (0).
    """
    removed = 0
    try:
        entries = os.listdir(directory)
    except OSError:
        return 0
    now = time.time()
    for entry in entries:
        if not entry.endswith(".lock"):
            continue
        path = os.path.join(directory, entry)
        try:
            if now - os.path.getmtime(path) >= stale_after:
                os.remove(path)
                removed += 1
        except OSError:
            continue
    return removed


def _silent_remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass
