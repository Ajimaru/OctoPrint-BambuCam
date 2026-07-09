"""Tests for octoprint_bambucam.ipcam_sync."""

# pylint: disable=protected-access

import contextlib
import json
import logging
import os
import threading
import time
from typing import Any
from unittest.mock import MagicMock

import pytest
from octoprint.events import Events

from octoprint_bambucam.ftp import FtpError
from octoprint_bambucam.ipcam_sync import IpcamSyncMixin, _gcode_name
from octoprint_bambucam.render_paths import RenderPaths


class FakeIpcamFtp:
    """Context-managed fake /ipcam service with a scripted listing/download."""

    def __init__(self, listing, fail_names=()):
        self._listing = listing
        self._fail_names = set(fail_names)
        self.downloaded = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def list_ipcam(self):
        """Return the scripted listing."""
        return list(self._listing)

    def download(self, name, dest, progress_cb=None):
        """Write a fake chunk unless the name is scripted to fail.

        Invokes ``progress_cb`` like the real client so a cancel-abort callback
        can raise mid-transfer; the resulting ``.part`` is cleaned on the raise,
        mirroring the real ``download``'s except path.
        """
        if name in self._fail_names:
            raise FtpError("network", "boom")
        part = dest + ".part"
        with open(part, "wb") as fh:
            fh.write(b"x" * 10)
        if progress_cb is not None:
            try:
                progress_cb(10, 10)
            except Exception:
                os.remove(part)
                raise
        os.replace(part, dest)
        self.downloaded.append(name)


class Host(IpcamSyncMixin):
    """Minimal host wiring the mixin to fakes."""

    # Loosen the mixin's PluginSettings/PluginManager annotations to Any: at
    # runtime these are MagicMocks, so the tests assert on mock attributes
    # (call_args_list, get_boolean) the real types do not expose. ``Any`` is
    # override-compatible with the base annotations, where ``MagicMock`` is
    # flagged by Pylance (reportIncompatibleVariableOverride).
    _settings: Any
    _plugin_manager: Any

    def __init__(self, tmp_path, ftp):
        self._logger = logging.getLogger("test.ipcam_sync")
        self._identifier = "bambucam"
        self._plugin_manager = MagicMock()
        self._settings = MagicMock()
        self._settings.get_boolean = MagicMock(return_value=True)
        self._settings.get_int = MagicMock(return_value=0)
        # print_jobs/print_dates back the manual-harvest job-name fallback;
        # default to empty so ``_last_print_job`` returns None (no crash).
        self._settings.get = MagicMock(return_value={})
        self._paths = RenderPaths(str(tmp_path))
        self._paths.ensure_dirs()
        self._ftp = ftp
        self._printing = False
        self.auto_rendered = []
        self.chunk_progress = []
        self._init_ipcam_sync()

    def _pipeline_chunk_progress(self, done, total):
        """Record harvest progress (provided by AutoSyncMixin at runtime)."""
        self.chunk_progress.append((done, total))

    def _pipeline_download_progress(self, transferred, total):
        """Record download speed samples (provided by AutoSyncMixin)."""

    @contextlib.contextmanager
    def _manual_harvest_ui(self):
        """Record that the manual-harvest UI busy flag was released."""
        try:
            yield
        finally:
            self.manual_ui_released = getattr(self, "manual_ui_released", 0) + 1

    @contextlib.contextmanager
    def _webcam_paused_for_harvest(self):
        """Record that the stream was paused for the pull (plugin hook)."""
        self.webcam_paused = getattr(self, "webcam_paused", 0) + 1
        yield

    def _render_paths(self):
        return self._paths

    def _make_ipcam_ftp(self):
        return self._ftp

    def _print_active(self):
        return self._printing

    def _auto_render_group(self, print_id):
        self.auto_rendered.append(print_id)


@pytest.fixture()
def tmp_path_str(tmp_path):
    """Return the tmp path."""
    return tmp_path


def _listing(*entries):
    """Build an /ipcam listing from (name, size, slot) tuples."""
    return [
        {"name": n, "size": s, "date": None, "slot": slot}
        for n, s, slot in entries
    ]


class TestDiff:
    """_diff_ipcam finds new/grown chunks since the baseline."""

    def test_new_chunks_detected(self, tmp_path):
        """Chunks absent from the baseline are returned, sorted by slot."""
        ftp = FakeIpcamFtp(_listing(("b.avi", 100, 6), ("a.avi", 100, 5)))
        host = Host(tmp_path, ftp)
        chunks = host._diff_ipcam({})
        assert [c["name"] for c in chunks] == ["a.avi", "b.avi"]

    def test_grown_chunk_detected(self, tmp_path):
        """A chunk whose size changed counts as this print's."""
        ftp = FakeIpcamFtp(_listing(("a.avi", 200, 5)))
        host = Host(tmp_path, ftp)
        baseline = {"a.avi": {"size": 100, "mdtm": None, "slot": 5}}
        assert len(host._diff_ipcam(baseline)) == 1

    def test_list_failure_returns_empty(self, tmp_path):
        """A listing FtpError yields no chunks."""
        ftp = FakeIpcamFtp([])

        def boom():
            raise FtpError("unreachable", "x")

        ftp.list_ipcam = boom
        host = Host(tmp_path, ftp)
        assert not host._diff_ipcam({})

    def test_no_baseline_caps_to_newest(self, tmp_path):
        """No baseline (None) caps to the newest few chunks, not all.

        A naive diff against an empty set would pull all ~160 ring-buffer slots
        (~17 GB); the cap keeps a manual/missed-baseline harvest small.
        """
        listing = [
            {
                "name": f"ipcam-record.{i:02d}.avi",
                "size": 100,
                "date": f"202606240{i:02d}000",
                "slot": i,
            }
            for i in range(20)
        ]
        ftp = FakeIpcamFtp(listing)
        host = Host(tmp_path, ftp)
        chunks = host._diff_ipcam(None)
        assert len(chunks) == 6
        # the newest by mdtm are slots 14..19
        assert {c["slot"] for c in chunks} == {14, 15, 16, 17, 18, 19}

    def test_empty_baseline_is_exact_diff(self, tmp_path):
        """A real empty baseline ({}) takes everything (genuine new footage)."""
        ftp = FakeIpcamFtp(_listing(("a.avi", 10, 5), ("b.avi", 10, 6)))
        host = Host(tmp_path, ftp)
        assert len(host._diff_ipcam({})) == 2


class TestDownloadGroup:
    """_download_group writes chunks + order.json and notifies."""

    def test_full_download_writes_order(self, tmp_path):
        """All chunks download and order.json is written."""
        ftp = FakeIpcamFtp(_listing(("a.avi", 10, 5), ("b.avi", 10, 6)))
        host = Host(tmp_path, ftp)
        pid = "2026-06-23_1432__gearbox"
        chunks = host._diff_ipcam({})
        host._download_group(pid, chunks, threading.Event())
        group = host._paths.group_dir(pid)
        order_file = host._paths.order_file(pid)
        assert group is not None and order_file is not None
        assert os.path.isfile(os.path.join(group, "a.avi"))
        with open(order_file, encoding="utf-8") as fh:
            order = json.load(fh)
        assert [o["file"] for o in order] == ["a.avi", "b.avi"]
        states = [
            c.args[1]["state"]
            for c in host._plugin_manager.send_plugin_message.call_args_list
        ]
        assert "done" in states

    def test_partial_download_marks_incomplete(self, tmp_path, monkeypatch):
        """A chunk that always fails leaves the group incomplete."""
        monkeypatch.setattr(
            "octoprint_bambucam.ipcam_sync._RETRY_BACKOFFS", (0, 0, 0)
        )
        ftp = FakeIpcamFtp(
            _listing(("a.avi", 10, 5), ("b.avi", 10, 6)),
            fail_names=["b.avi"],
        )
        host = Host(tmp_path, ftp)
        pid = "2026-06-23_1432__gearbox"
        chunks = host._diff_ipcam({})
        host._download_group(pid, chunks, threading.Event())
        states = [
            c.args[1]["state"]
            for c in host._plugin_manager.send_plugin_message.call_args_list
        ]
        assert "failed" in states

    def test_no_space_skips(self, tmp_path, monkeypatch):
        """A disk-space shortfall fails the group before downloading."""
        ftp = FakeIpcamFtp(_listing(("a.avi", 10**12, 5)))
        host = Host(tmp_path, ftp)

        class FakeUsage:
            """Stub for shutil.disk_usage with no free space."""

            free = 1

        monkeypatch.setattr(
            "octoprint_bambucam.ipcam_sync.shutil.disk_usage",
            lambda p: FakeUsage(),
        )
        pid = "2026-06-23_1432__gearbox"
        chunks = host._diff_ipcam({})
        host._download_group(pid, chunks, threading.Event())
        reasons = [
            c.args[1].get("reason")
            for c in host._plugin_manager.send_plugin_message.call_args_list
        ]
        assert "no_space" in reasons

    def test_full_download_triggers_auto_render(self, tmp_path):
        """A complete download hands the group to the auto-render hook."""
        ftp = FakeIpcamFtp(_listing(("a.avi", 10, 5)))
        host = Host(tmp_path, ftp)
        pid = "2026-06-23_1432__gearbox"
        host._download_group(pid, host._diff_ipcam({}), threading.Event())
        assert host.auto_rendered == [pid]

    def test_partial_download_no_auto_render(self, tmp_path, monkeypatch):
        """An incomplete download must not trigger auto-render."""
        monkeypatch.setattr(
            "octoprint_bambucam.ipcam_sync._RETRY_BACKOFFS", (0, 0, 0)
        )
        ftp = FakeIpcamFtp(_listing(("a.avi", 10, 5)), fail_names=["a.avi"])
        host = Host(tmp_path, ftp)
        pid = "2026-06-23_1432__gearbox"
        host._download_group(pid, host._diff_ipcam({}), threading.Event())
        assert host.auto_rendered == []

    def test_clears_stale_part_files(self, tmp_path):
        """A leftover .part from a prior interrupted harvest is removed."""
        ftp = FakeIpcamFtp(_listing(("a.avi", 10, 5)))
        host = Host(tmp_path, ftp)
        pid = "2026-06-23_1432__gearbox"
        group = host._paths.group_dir(pid)
        assert group is not None
        os.makedirs(group, exist_ok=True)
        stale = os.path.join(group, "old.avi.part")
        with open(stale, "wb") as fh:
            fh.write(b"stale")
        host._download_group(pid, host._diff_ipcam({}), threading.Event())
        assert not os.path.exists(stale)

    def test_cancel_aborts_download_no_part_left(self, tmp_path):
        """A cancel mid-transfer aborts and leaves no .part behind."""
        ftp = FakeIpcamFtp(_listing(("a.avi", 10, 5)))
        host = Host(tmp_path, ftp)
        cancel = threading.Event()
        cancel.set()
        pid = "2026-06-23_1432__gearbox"
        host._download_group(pid, host._diff_ipcam({}), cancel)
        group = host._paths.group_dir(pid)
        assert group is not None
        assert not os.path.exists(os.path.join(group, "a.avi.part"))
        assert not os.path.exists(os.path.join(group, "a.avi"))
        assert not ftp.downloaded


class TestEventsAndId:
    """on_ipcam_event and print-id uniqueness."""

    def test_unique_print_id_collision(self, tmp_path):
        """A same-minute collision gets a -N suffix."""
        ftp = FakeIpcamFtp([])
        host = Host(tmp_path, ftp)
        base = "2026-06-23_1432__gearbox"
        base_group = host._paths.group_dir(base)
        assert base_group is not None
        os.makedirs(base_group)
        pid = host._unique_print_id("2026-06-23 14:32", "gearbox.gcode")
        assert pid == base + "-1"

    def test_print_started_baselines(self, tmp_path):
        """PrintStarted snapshots a baseline (no harvest scheduled here)."""
        ftp = FakeIpcamFtp(_listing(("a.avi", 10, 5)))
        host = Host(tmp_path, ftp)
        host.on_ipcam_event(Events.PRINT_STARTED, {})
        # baseline snapshot runs in a thread; just assert no crash + cancel slot
        assert host._ipcam_cancel is None or isinstance(
            host._ipcam_cancel, threading.Event
        )

    def test_print_done_does_not_harvest_here(self, tmp_path):
        """PRINT_DONE no longer triggers a harvest in on_ipcam_event.

        The harvest is now stage 3 of the post-print pipeline; this handler
        only baselines on PRINT_STARTED.
        """
        ftp = FakeIpcamFtp(_listing(("a.avi", 10, 5)))
        host = Host(tmp_path, ftp)
        host.on_ipcam_event(Events.PRINT_DONE, {})  # must not raise / download
        assert not ftp.downloaded

    def test_disabled_setting_noop(self, tmp_path):
        """With auto_download off, events do nothing."""
        ftp = FakeIpcamFtp([])
        host = Host(tmp_path, ftp)
        host._settings.get_boolean = MagicMock(return_value=False)
        host.on_ipcam_event(Events.PRINT_STARTED, {})  # must not raise

    def test_harvest_now_returns_ok(self, tmp_path):
        """harvest_now returns ok and its worker releases the UI busy flag.

        Regression: a manual harvest raised the pipeline ``busy`` flag via the
        chunk-progress callback but never lowered it (it does not run through
        ``_post_print_worker``), so "Harvesting n/n" and the Timelapse "Copy in
        progress" banner stayed up forever.
        """
        ftp = FakeIpcamFtp([])
        host = Host(tmp_path, ftp)
        assert host.harvest_now()["ok"] is True
        # the worker runs on a daemon thread; wait briefly for it to finish
        for _ in range(200):
            if getattr(host, "manual_ui_released", 0):
                break
            time.sleep(0.005)
        assert getattr(host, "manual_ui_released", 0) == 1

    def test_run_ipcam_harvest_downloads(self, tmp_path):
        """_run_ipcam_harvest (pipeline stage 3) diffs and downloads."""
        ftp = FakeIpcamFtp(_listing(("a.avi", 10, 5)))
        host = Host(tmp_path, ftp)
        # _init_ipcam_sync leaves no baseline → the newest-chunks cap applies
        assert host._ipcam_baseline is None
        host._run_ipcam_harvest({"name": "gearbox.gcode"}, threading.Event())
        assert ftp.downloaded == ["a.avi"]

    def test_harvest_no_chunks_returns(self, tmp_path):
        """An empty diff ends the harvest without downloading.

        It still emits a terminal ``done`` (count 0) so a client that showed a
        persistent "fetching" toast can drop it instead of hanging forever.
        """
        ftp = FakeIpcamFtp([])
        host = Host(tmp_path, ftp)
        host._harvest(
            "2026-06-23 14:32", "gearbox.gcode", {}, threading.Event()
        )
        assert not ftp.downloaded
        pushes = [
            c.args[1]
            for c in host._plugin_manager.send_plugin_message.call_args_list
            if c.args[1].get("type") == "ipcam_download"
        ]
        assert pushes and pushes[-1]["state"] == "done"
        assert pushes[-1]["count"] == 0

    def test_failed_download_discards_empty_group(self, tmp_path, monkeypatch):
        """A harvest that saves nothing removes the group dir (no phantom)."""
        # collapse the retry backoffs so the failing download returns fast
        monkeypatch.setattr("octoprint_bambucam.ipcam_sync._RETRY_BACKOFFS", ())
        ftp = FakeIpcamFtp(_listing(("a.avi", 10, 5)), fail_names={"a.avi"})
        host = Host(tmp_path, ftp)
        host._run_ipcam_harvest({"name": "x.gcode"}, threading.Event())
        # group dir must not linger with an empty order.json
        groups = os.listdir(host._paths.raw_chunks_dir)
        assert groups == []

    def test_harvest_reports_chunk_progress(self, tmp_path):
        """Stage 3 mirrors done/total chunk progress to the UI bar."""
        ftp = FakeIpcamFtp(_listing(("a.avi", 10, 5)))
        host = Host(tmp_path, ftp)
        host._run_ipcam_harvest({"name": "x.gcode"}, threading.Event())
        # progress reported at least the final (done, total) tuple
        assert host.chunk_progress
        assert host.chunk_progress[-1] == (1, 1)

    def test_harvest_pauses_webcam_stream(self, tmp_path):
        """The pull is wrapped in the webcam-pause context (bandwidth)."""
        ftp = FakeIpcamFtp(_listing(("a.avi", 10, 5)))
        host = Host(tmp_path, ftp)
        host._run_ipcam_harvest({"name": "x.gcode"}, threading.Event())
        assert getattr(host, "webcam_paused", 0) == 1

    def test_last_print_job_picks_newest(self, tmp_path):
        """_last_print_job returns the most recently dated recorded job."""
        ftp = FakeIpcamFtp([])
        host = Host(tmp_path, ftp)
        jobs = {"vid_old.avi": "old.gcode", "vid_new.avi": "new.gcode"}
        dates = {
            "vid_old.avi": "2026-01-01 10:00",
            "vid_new.avi": "2026-07-05 14:38",
        }

        def _get(path):
            return {"print_jobs": jobs, "print_dates": dates}.get(path[0], {})

        host._settings.get = _get
        assert host._last_print_job() == "new.gcode"

    def test_manual_harvest_uses_last_job_name(self, tmp_path):
        """harvest_now falls back to the last recorded job name."""
        ftp = FakeIpcamFtp(_listing(("a.avi", 10, 5)))
        host = Host(tmp_path, ftp)

        def _get(path):
            return {
                "print_jobs": {"v.avi": "toolbox.gcode.3mf"},
                "print_dates": {"v.avi": "2026-07-05 14:38"},
            }.get(path[0], {})

        host._settings.get = _get
        captured = {}
        done = threading.Event()
        orig = host._harvest

        def _spy(when, gcode, baseline, cancel):
            captured["gcode"] = gcode
            try:
                return orig(when, gcode, baseline, cancel)
            finally:
                done.set()

        host._harvest = _spy  # type: ignore[method-assign]
        host.harvest_now()
        assert done.wait(timeout=5)
        assert captured.get("gcode") == "toolbox.gcode.3mf"


class TestCancelHarvest:
    """handle_cancel_harvest + the running-harvest cancel registration."""

    def test_not_running(self, tmp_path):
        """Without a running harvest the API reports not_running."""
        import flask as _flask

        host = Host(tmp_path, FakeIpcamFtp([]))
        app = _flask.Flask(__name__)
        with app.test_request_context():
            payload = host.handle_cancel_harvest().get_json()
        assert payload == {"ok": False, "reason": "not_running"}

    def test_sets_running_event(self, tmp_path):
        """With a registered harvest the API sets its cancel event."""
        import flask as _flask

        host = Host(tmp_path, FakeIpcamFtp([]))
        cancel = threading.Event()
        with host._ipcam_lock:  # register like a running _harvest
            host._ipcam_cancel = cancel
        app = _flask.Flask(__name__)
        with app.test_request_context():
            payload = host.handle_cancel_harvest().get_json()
        assert payload["ok"] is True
        assert cancel.is_set()

    def test_harvest_registers_and_clears_cancel(self, tmp_path):
        """_harvest exposes its cancel event while running, clears it after."""
        ftp = FakeIpcamFtp(_listing(("a.avi", 10, 5)))
        host = Host(tmp_path, ftp)
        seen = []
        host._pipeline_download_progress = (  # type: ignore[method-assign]
            lambda transferred, total: seen.append(host._ipcam_cancel)
        )
        cancel = threading.Event()
        host._harvest("2026-07-08 10:00", "gearbox.gcode", None, cancel)
        # the mid-download callback saw the registered event…
        assert seen and all(ev is cancel for ev in seen)
        # …and after the harvest the slot is cleared again
        assert host._ipcam_cancel is None

    def test_cancelled_harvest_keeps_saved_chunks(self, tmp_path):
        """Cancelling between chunks keeps what was saved (incomplete group).

        The cancel event is set from the first chunk's progress callback —
        like a Stop click during a long transfer. The first chunk was already
        replaced into place by then, so it stays; the second is never pulled
        and the terminal push reports the cancelled reason with got/want.
        """
        ftp = FakeIpcamFtp(_listing(("a.avi", 10, 1), ("b.avi", 10, 2)))
        host = Host(tmp_path, ftp)
        cancel = threading.Event()
        host._pipeline_download_progress = (  # type: ignore[method-assign]
            lambda transferred, total: cancel.set()
        )
        host._harvest("2026-07-08 10:00", "gearbox.gcode", None, cancel)
        assert ftp.downloaded == ["a.avi"]
        pushes = [
            c.args[1]
            for c in host._plugin_manager.send_plugin_message.call_args_list
            if c.args[1].get("type") == "ipcam_download"
        ]
        assert pushes[-1]["state"] == "failed"
        assert pushes[-1]["reason"] == "cancelled"
        assert pushes[-1]["got"] == 1
        assert pushes[-1]["want"] == 2


class TestGcodeName:
    """_gcode_name extracts the file name from a payload."""

    def test_name_then_path(self):
        """name wins, then path, then None."""
        assert _gcode_name({"name": "a.gcode"}) == "a.gcode"
        assert _gcode_name({"path": "b.gcode"}) == "b.gcode"
        assert _gcode_name({}) is None
        assert _gcode_name(None) is None
