"""Single-worker render job queue with idle-gating and recovery.

Runs at most one render job at a time (a Pi cannot afford parallel x264
encodes, plan §3.5). Jobs wait in the :class:`render_registry.JobRegistry`;
the worker thread starts a job only when the idle gate is open (printer not
printing, OctoPrint not rendering, no FTP batch in flight). Cancel kills the
running ffmpeg; recovery on startup clears the ``work/`` scratch dir, reclaims
stale locks, and fails any job a crash left mid-render.

The actual ffmpeg work (concat + render) is supplied as a ``run_job`` callable
so this module stays free of subprocess details and unit-testable.
"""

import logging
import os
import shutil
import threading
import uuid
from typing import Callable, Optional

from . import render_registry
from .render_registry import (
    JOB_CANCELLED,
    JOB_CONCAT,
    JOB_DONE,
    JOB_FAILED,
    JOB_QUEUED,
    JobRegistry,
    clear_stale_locks,
)

_GATE_POLL_SECONDS = 5


class RenderQueue:
    """Serialize render jobs behind an idle gate and a single worker thread.

    ``run_job(job, cancel, progress_cb)`` performs the encode and returns the
    final output path (or raises). ``gate_open()`` reports system idleness;
    ``notify(job, state, **extra)`` pushes progress to the UI. Construct one
    per plugin instance and call :meth:`start`/:meth:`recover` from startup.
    """

    def __init__(
        self,
        logger: logging.Logger,
        paths,
        registry: JobRegistry,
        *,
        run_job: Callable,
        gate_open: Callable[[], bool],
        notify: Callable,
        on_group_rendered: Optional[Callable[[str], None]] = None,
        max_queue_size: int = 10,
    ):
        self._logger = logger
        self._paths = paths
        self._registry = registry
        self._run_job = run_job
        self._gate_open = gate_open
        self._notify = notify
        self._on_group_rendered = on_group_rendered
        self._max_queue_size = max_queue_size
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._worker: Optional[threading.Thread] = None
        self._active_cancel: Optional[threading.Event] = None
        self._active_jobid: Optional[str] = None
        self._lock = threading.Lock()

    def enqueue(self, print_id: str, preset: str, chunks: list) -> dict:
        """Queue a render job for a group; return ``{ok, jobid|reason}``.

        Rejects a second job for the same group (one render per group at a
        time) and a queue that is already at capacity.
        """
        if self._registry.active_for_group(print_id) is not None:
            return {"ok": False, "reason": "already_queued"}
        if self._queued_count() >= self._max_queue_size:
            return {"ok": False, "reason": "queue_full"}
        jobid = uuid.uuid4().hex
        job = {
            "jobid": jobid,
            "print_id": print_id,
            "preset": preset,
            "chunks": list(chunks),
            "state": JOB_QUEUED,
            "percent": 0,
        }
        self._registry.add(job)
        self._notify(job, JOB_QUEUED)
        self._wake.set()
        return {"ok": True, "jobid": jobid}

    def cancel(self, jobid: str) -> dict:
        """Cancel a queued or running job; return ``{ok, reason?}``."""
        job = self._registry.get(jobid)
        if job is None:
            return {"ok": False, "reason": "unknown_job"}
        with self._lock:
            if self._active_jobid == jobid and self._active_cancel:
                self._active_cancel.set()
        if job["state"] == JOB_QUEUED:
            self._registry.update(jobid, state=JOB_CANCELLED)
            self._notify(self._registry.get(jobid), JOB_CANCELLED)
        return {"ok": True}

    def start(self) -> None:
        """Start the worker thread (idempotent)."""
        if self._worker is not None and self._worker.is_alive():
            return
        self._stop.clear()
        self._worker = threading.Thread(target=self._loop, daemon=True)
        self._worker.start()

    def stop(self) -> None:
        """Signal the worker to exit and cancel any running job."""
        self._stop.set()
        with self._lock:
            if self._active_cancel:
                self._active_cancel.set()
        self._wake.set()

    def recover(self, stale_lock_timeout: int) -> None:
        """Startup recovery (plan §3.5/§12): clean work, locks, dead jobs.

        Removes everything under ``work/``, reclaims ``*.lock`` files older
        than ``stale_lock_timeout`` seconds, and fails any job a crash left in
        a running state (its group falls back to ``chunks_ready`` via the
        scan). Re-queues nothing — the user restarts renders manually.
        """
        self._clear_work_dir()
        clear_stale_locks(self._paths.metadata_dir, stale_lock_timeout)
        clear_stale_locks(self._paths.work_dir, stale_lock_timeout)
        for jobid in self._registry.reconcile_active():
            self._logger.info("recovery: failed interrupted job %s", jobid)

    def jobs(self) -> list:
        """Return the active jobs (queued/running) for the UI queue table.

        Terminal jobs (``done``/``failed``/``cancelled``) are not live work —
        a finished group leaves the Raw Files tab entirely — so they are pruned
        from the registry here rather than lingering in ``jobs.json`` and the
        render-queue table forever.
        """
        self._prune_terminal_jobs()
        return [
            j
            for j in self._registry.all()
            if j.get("state") in render_registry.ACTIVE_JOB_STATES
        ]

    def _prune_terminal_jobs(self) -> None:
        for job in self._registry.all():
            if job.get("state") not in render_registry.ACTIVE_JOB_STATES:
                self._registry.remove(job["jobid"])

    def jobs_active_for(self, print_id: str) -> bool:
        """True when a queued/running job holds this group (blocks delete)."""
        return self._registry.active_for_group(print_id) is not None

    def _queued_count(self) -> int:
        return sum(
            1
            for j in self._registry.all()
            if j.get("state") in render_registry.ACTIVE_JOB_STATES
        )

    def _clear_work_dir(self) -> None:
        work = self._paths.work_dir
        try:
            entries = os.listdir(work)
        except OSError:
            return
        for entry in entries:
            path = os.path.join(work, entry)
            try:
                if os.path.isdir(path):
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    os.remove(path)
            except OSError:
                self._logger.debug("could not remove work entry %s", path)

    def _loop(self) -> None:
        while not self._stop.is_set():
            job = self._next_runnable()
            if job is None:
                self._wake.wait(timeout=_GATE_POLL_SECONDS)
                self._wake.clear()
                continue
            self._run_one(job)

    def _next_runnable(self) -> Optional[dict]:
        """Pick the oldest queued job, but only when the gate is open."""
        queued = [
            j for j in self._registry.all() if j.get("state") == JOB_QUEUED
        ]
        if not queued:
            return None
        if not self._gate_open():
            return None
        return queued[0]

    def _run_one(self, job: dict) -> None:
        jobid = job["jobid"]
        cancel = threading.Event()
        with self._lock:
            self._active_cancel = cancel
            self._active_jobid = jobid
        self._registry.update(jobid, state=JOB_CONCAT, percent=0)

        def _progress(phase: str, percent: int) -> None:
            self._registry.update(jobid, state=phase, percent=percent)
            current = self._registry.get(jobid)
            if current is not None:
                self._notify(current, phase, percent=percent)

        try:
            output = self._run_job(job, cancel, _progress)
        except Exception as exc:  # noqa: BLE001 - any failure → job failed
            self._finish_failed(jobid, exc, cancel)
        else:
            self._finish_done(jobid, output)
        finally:
            with self._lock:
                self._active_cancel = None
                self._active_jobid = None

    def _finish_done(self, jobid: str, output: str) -> None:
        job = self._registry.update(
            jobid, state=JOB_DONE, percent=100, output=output
        )
        self._notify(job, JOB_DONE, output=output)
        if self._on_group_rendered and job is not None:
            self._on_group_rendered(job["print_id"])

    def _finish_failed(self, jobid: str, exc, cancel) -> None:
        if cancel.is_set():
            job = self._registry.update(jobid, state=JOB_CANCELLED)
            self._notify(job, JOB_CANCELLED)
            return
        reason = getattr(exc, "reason", "error")
        self._logger.warning("render job %s failed: %s", jobid, exc)
        job = self._registry.update(jobid, state=JOB_FAILED, reason=reason)
        self._notify(job, JOB_FAILED, reason=reason)
