"""Tests for automatic timelapse pull after a print (AutoSyncMixin)."""

# pylint: disable=protected-access,redefined-outer-name,too-few-public-methods
# pylint: disable=attribute-defined-outside-init

import logging
import threading

from octoprint.events import Events

from octoprint_bambucam.autosync import AutoSyncMixin


class _FakeSettings:
    """Minimal in-memory stand-in for OctoPrint's ``PluginSettings``."""

    def __init__(self, values):
        self._v = dict(values)
        self.saved = False

    def get(self, path):
        """Return the value stored under ``path[0]``."""
        return self._v.get(path[0])

    def get_boolean(self, path):
        """Return the value under ``path[0]`` coerced to ``bool``."""
        return bool(self._v.get(path[0]))

    def get_int(self, path):
        """Return ``path[0]`` coerced to ``int`` (0 if unset)."""
        return int(self._v.get(path[0]) or 0)

    def set(self, path, value):
        """Store ``value`` under ``path[0]``."""
        self._v[path[0]] = value

    def save(self):
        """Record that a save happened (so tests can assert on it)."""
        self.saved = True


class _FakeFtp:
    """Context-manager FTP stand-in that lists a fixed set of names."""

    def __init__(self, names):
        self._names = names

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def list_timelapses(self):
        """Return the configured names as listing records."""
        return [{"name": n} for n in self._names]


class _Host(AutoSyncMixin):
    """Concrete AutoSyncMixin with the deps BambucamPlugin would provide."""

    def __init__(self, settings_values, *, sd_names=None, copied=None):
        settings = _FakeSettings(settings_values)
        self._settings = settings  # type: ignore[assignment]
        self._logger = logging.getLogger("test.autosync")
        self._identifier = "bambucam"
        self._ftp_busy = False
        self._printing = False
        self._sd_names = sd_names or []
        self._copied = set(copied or [])
        self.sent = []
        self.ran = []  # (op, names) from _run_batch
        self._init_autosync()

    # plugin/messaging
    class _PM:
        """Plugin-manager stand-in that records sent plugin messages."""

        def __init__(self, outer):
            self._outer = outer

        def send_plugin_message(self, _ident, msg):
            """Record ``msg``; the identifier is irrelevant to the tests."""
            self._outer.sent.append(msg)

    @property
    def _plugin_manager(self):  # type: ignore[override]
        """Return a fresh recording plugin-manager stand-in."""
        return self._PM(self)

    # deps from TimelapseOpsMixin
    def _print_active(self):
        """Whether a print is currently active (drives the idle gate)."""
        return self._printing

    def _already_copied(self, name):
        """Whether ``name`` was already pulled locally."""
        return name in self._copied

    def _make_ftp(self):
        """Return the FTP stand-in exposing the configured SD-card names."""
        return _FakeFtp(self._sd_names)

    def _run_batch(self, op, names):
        """Record the ``(op, names)`` batch instead of transferring files."""
        self.ran.append((op, names))


def _settings(**over):
    """Build a default auto-sync settings dict, overridable via kwargs."""
    base = {
        "auto_sync": True,
        "auto_sync_delay": 0,
        "auto_sync_action": "copy",
    }
    base.update(over)
    return base


# ── gate ──────────────────────────────────────────────────────────────────


def test_gate_open_when_idle():
    """The idle gate is open when nothing is printing/rendering/transferring."""
    host = _Host(_settings())
    assert host._gate_open() is True


def test_gate_closed_while_printing():
    """An active print keeps the idle gate closed."""
    host = _Host(_settings())
    host._printing = True
    assert host._gate_open() is False


def test_gate_closed_while_octoprint_rendering():
    """An in-progress OctoPrint render keeps the idle gate closed."""
    host = _Host(_settings())
    host._octoprint_rendering = True
    assert host._gate_open() is False


def test_gate_closed_while_ftp_busy():
    """A manual FTP batch in flight keeps the idle gate closed."""
    host = _Host(_settings())
    host._ftp_busy = True
    assert host._gate_open() is False


# ── render flag + self-trigger guard ──────────────────────────────────────


def test_movie_rendering_sets_flag():
    """MOVIE_RENDERING sets the render flag; MOVIE_DONE clears it."""
    host = _Host(_settings())
    host.on_event(Events.MOVIE_RENDERING, {"movie": "/tl/octoprint.mp4"})
    assert host._octoprint_rendering is True
    host.on_event(Events.MOVIE_DONE, {"movie": "/tl/octoprint.mp4"})
    assert host._octoprint_rendering is False


def test_movie_failed_clears_flag():
    """MOVIE_FAILED clears the render flag."""
    host = _Host(_settings())
    host._octoprint_rendering = True
    host.on_event(Events.MOVIE_FAILED, {"movie": "/tl/x.mp4"})
    assert host._octoprint_rendering is False


def test_own_movie_does_not_toggle_flag():
    """Our own MovieDone must not flip the flag and consumes its marker."""
    host = _Host(_settings())
    host.note_own_movie("/tl/bambu.mp4")
    # our own MovieDone must NOT clear/flip the flag (no self-trigger)
    host._octoprint_rendering = True
    host.on_event(Events.MOVIE_DONE, {"movie": "/tl/bambu.mp4"})
    assert host._octoprint_rendering is True
    # and the marker is consumed so the set cannot grow unbounded
    assert host._is_own_movie({"movie": "/tl/bambu.mp4"}) is False


def test_own_movie_rendering_event_ignored():
    """A MOVIE_RENDERING for our own movie does not set the render flag."""
    host = _Host(_settings())
    host.note_own_movie("/tl/bambu.mp4")
    host.on_event(Events.MOVIE_RENDERING, {"movie": "/tl/bambu.mp4"})
    assert host._octoprint_rendering is False


# ── new-file filter ───────────────────────────────────────────────────────


def test_list_new_ignores_name_timestamp():
    """No name-timestamp high-water mark: an SD name carrying an *older*
    timestamp than another is still returned as new, because the A1 mini
    stamps timelapse files with a frozen/wrong date in LAN-only mode. Only
    ``_already_copied`` filters."""
    host = _Host(
        _settings(),
        sd_names=[
            "video_2026-05-18_03-36-28.avi",  # "older" name but not copied
            "video_2026-05-18_01-00-00.avi",  # even "older" name, not copied
            "video_2026-05-18_06-50-48.avi",  # "newer" name, not copied
        ],
    )
    # all three are returned (order not significant) — none filtered by ts
    assert set(host._list_new_timelapses()) == {
        "video_2026-05-18_03-36-28.avi",
        "video_2026-05-18_01-00-00.avi",
        "video_2026-05-18_06-50-48.avi",
    }


def test_list_new_skips_already_copied():
    """Names already present locally are excluded from the new list."""
    host = _Host(
        _settings(),
        sd_names=["video_2026-05-18_06-50-48.avi"],
        copied=["video_2026-05-18_06-50-48.avi"],
    )
    assert host._list_new_timelapses() == []


def test_list_new_returns_all_uncopied():
    """Every SD name not already present locally is returned (order is not
    significant since the printer timestamps are unreliable)."""
    host = _Host(
        _settings(),
        sd_names=[
            "video_2026-05-18_06-50-48.avi",
            "video_2026-05-18_03-36-28.avi",
        ],
    )
    assert set(host._list_new_timelapses()) == {
        "video_2026-05-18_03-36-28.avi",
        "video_2026-05-18_06-50-48.avi",
    }


# ── pull ──────────────────────────────────────────────────────────────────


def test_do_autosync_runs_batch_and_notifies():
    """A pull runs the batch over all uncopied names and notifies."""
    host = _Host(
        _settings(auto_sync_action="move"),
        sd_names=[
            "video_2026-05-18_03-36-28.avi",
            "video_2026-05-18_06-50-48.avi",
        ],
    )
    host._do_autosync()
    assert len(host.ran) == 1
    op, names = host.ran[0]
    assert op == "move"
    assert set(names) == {
        "video_2026-05-18_03-36-28.avi",
        "video_2026-05-18_06-50-48.avi",
    }
    # user got a notification
    assert any(m["type"] == "auto_sync" for m in host.sent)


def test_do_autosync_noop_when_nothing_new():
    """With no new names, no batch runs and no message is sent."""
    host = _Host(_settings(), sd_names=[])
    host._do_autosync()
    assert not host.ran
    assert not host.sent


def test_invalid_action_falls_back_to_copy():
    """An unknown auto-sync action falls back to ``copy``."""
    host = _Host(
        _settings(auto_sync_action="bogus"),
        sd_names=["video_2026-05-18_06-50-48.avi"],
    )
    host._do_autosync()
    assert host.ran[0][0] == "copy"


# ── trigger / abort ───────────────────────────────────────────────────────


def test_print_done_off_does_nothing():
    """With auto-sync disabled, PRINT_DONE schedules nothing."""
    host = _Host(
        _settings(auto_sync=False),
        sd_names=["video_2026-05-18_06-50-48.avi"],
    )
    host._on_print_done()
    # no worker thread scheduled → nothing pulled
    assert host._autosync_cancel is None
    assert not host.ran


def test_print_started_cancels_pending():
    """PRINT_STARTED cancels a still-pending auto-sync attempt."""
    host = _Host(_settings())
    cancel = threading.Event()
    host._autosync_cancel = cancel
    host.on_event(Events.PRINT_STARTED, {})
    assert cancel.is_set()
    assert host._autosync_cancel is None


def test_wait_until_idle_returns_false_on_cancel():
    """Waiting for idle returns False when the cancel event is set."""
    host = _Host(_settings())
    host._printing = True  # gate never opens
    cancel = threading.Event()
    cancel.set()
    assert host._wait_until_idle(cancel) is False


def test_wait_until_idle_true_when_already_idle():
    """Waiting for idle returns True immediately when already idle."""
    host = _Host(_settings())
    assert host._wait_until_idle(threading.Event()) is True


def test_worker_aborts_during_delay():
    """A worker whose cancel is already set aborts before pulling."""
    host = _Host(_settings(), sd_names=["video_2026-05-18_06-50-48.avi"])
    cancel = threading.Event()
    cancel.set()  # already cancelled → returns before pulling
    host._autosync_worker(delay=0, cancel=cancel)
    assert not host.ran


def test_worker_pulls_when_idle():
    """A worker pulls the new timelapse once the system is idle."""
    host = _Host(_settings(), sd_names=["video_2026-05-18_06-50-48.avi"])
    host._autosync_worker(delay=0, cancel=threading.Event())
    assert host.ran == [("copy", ["video_2026-05-18_06-50-48.avi"])]


def test_print_done_schedules_and_pulls(monkeypatch):
    """PRINT_DONE schedules a worker that runs and pulls the new file."""
    host = _Host(_settings(), sd_names=["video_2026-05-18_06-50-48.avi"])
    # run the worker synchronously instead of in a daemon thread
    started = []

    class _SyncThread:
        """Thread stand-in that runs ``target`` synchronously on ``start()``."""

        def __init__(self, target, args, **_kwargs):
            self._target = target
            self._args = args

        def start(self):
            """Record the start and run the target synchronously."""
            started.append(True)
            self._target(*self._args)

    monkeypatch.setattr(
        "octoprint_bambucam.autosync.threading.Thread", _SyncThread
    )
    host._on_print_done()
    assert started == [True]
    assert host.ran == [("copy", ["video_2026-05-18_06-50-48.avi"])]


def test_print_done_supersedes_pending():
    """A fresh PRINT_DONE cancels the previously-pending attempt."""
    host = _Host(_settings())
    old = threading.Event()
    host._autosync_cancel = old
    # a new print-done must cancel the still-pending attempt
    host._settings.set(["auto_sync"], True)
    host._on_print_done()
    assert old.is_set()


# ── real print-date recorder ──────────────────────────────────────────────


def _no_wait(monkeypatch):
    """Make the recorder loop tick instantly (no real sleeping/waiting)."""
    ticks = iter(range(0, 100000, 100))
    monkeypatch.setattr(
        "octoprint_bambucam.autosync.time.sleep", lambda _s: None
    )
    monkeypatch.setattr(
        "octoprint_bambucam.autosync.time.monotonic", lambda: next(ticks)
    )


def test_record_print_date_stamps_new_video(monkeypatch):
    """A video that appears after the baseline is stamped with the real time."""
    host = _Host(_settings(print_dates={}))
    _no_wait(monkeypatch)
    baseline = {"video_2026-05-18_03-02-39.avi": 100}
    host._snapshot_videos = lambda: {
        "video_2026-05-18_03-02-39.avi": 100,
        "video_2026-05-18_06-26-58.avi": 8363434,
    }
    host._record_print_date_worker("2026-06-22 14:42", baseline)
    stored = host._settings.get(["print_dates"])
    assert stored == {"video_2026-05-18_06-26-58.avi": "2026-06-22 14:42"}
    assert host._settings.saved is True


def test_record_print_date_stamps_grown_video(monkeypatch):
    """A video already present at baseline but that GREW (the printer was still
    writing it during the print) is recognized as this print's video."""
    host = _Host(_settings(print_dates={}))
    _no_wait(monkeypatch)
    baseline = {"video_2026-05-18_06-26-58.avi": 4096}  # half-written
    host._snapshot_videos = lambda: {
        "video_2026-05-18_06-26-58.avi": 8363434,  # finished
    }
    host._record_print_date_worker("2026-06-22 14:42", baseline)
    assert host._settings.get(["print_dates"]) == {
        "video_2026-05-18_06-26-58.avi": "2026-06-22 14:42"
    }


def test_record_print_date_no_change(monkeypatch):
    """If nothing is new or grown within the window, nothing is recorded."""
    host = _Host(_settings(print_dates={}))
    seq = iter([0, 1, 9_999_999, 9_999_999])
    monkeypatch.setattr(
        "octoprint_bambucam.autosync.time.sleep", lambda _s: None
    )
    monkeypatch.setattr(
        "octoprint_bambucam.autosync.time.monotonic", lambda: next(seq)
    )
    baseline = {"video_2026-05-18_03-02-39.avi": 100}
    host._snapshot_videos = lambda: {"video_2026-05-18_03-02-39.avi": 100}
    host._record_print_date_worker("2026-06-22 14:42", baseline)
    assert host._settings.get(["print_dates"]) == {}
    assert host._settings.saved is False


def test_record_print_date_spawns_worker(monkeypatch):
    """PRINT_DONE's recorder spawns a worker that records the print date,
    passing the PrintStarted baseline."""
    host = _Host(_settings(print_dates={}))
    host._print_baseline = {"video_2026-05-18_03-02-39.avi": 100}
    _no_wait(monkeypatch)

    class _SyncThread:
        def __init__(self, target, args, **_kwargs):
            self._target = target
            self._args = args

        def start(self):
            self._target(*self._args)

    monkeypatch.setattr(
        "octoprint_bambucam.autosync.threading.Thread", _SyncThread
    )
    host._snapshot_videos = lambda: {
        "video_2026-05-18_03-02-39.avi": 100,
        "video_2026-05-18_06-26-58.avi": 8363434,
    }
    host._record_print_date()
    assert "video_2026-05-18_06-26-58.avi" in host._settings.get(
        ["print_dates"]
    )


def test_record_print_date_no_baseline_falls_back(monkeypatch):
    """With no PrintStarted baseline, the worker snapshots now as baseline."""
    host = _Host(_settings(print_dates={}))
    _no_wait(monkeypatch)
    snaps = iter(
        [
            {"video_2026-05-18_03-02-39.avi": 100},  # fallback baseline
            {
                "video_2026-05-18_03-02-39.avi": 100,
                "video_2026-05-18_06-26-58.avi": 8363434,
            },
        ]
    )
    host._snapshot_videos = lambda: next(snaps)
    host._record_print_date_worker("2026-06-22 14:42", None)
    assert "video_2026-05-18_06-26-58.avi" in host._settings.get(
        ["print_dates"]
    )


def test_record_print_date_initial_list_fails(monkeypatch):
    """If the fallback baseline listing fails, the recorder gives up cleanly."""
    from octoprint_bambucam.ftp import FtpError

    host = _Host(_settings(print_dates={}))

    def _boom():
        raise FtpError("unreachable")

    host._snapshot_videos = _boom
    host._record_print_date_worker("2026-06-22 14:42", None)
    assert host._settings.get(["print_dates"]) == {}
    assert host._settings.saved is False


def test_snapshot_print_baseline_records_sizes():
    """``_snapshot_print_baseline`` stores name->size for current SD card."""
    host = _Host(
        _settings(),
        sd_names=[
            "video_2026-05-18_03-02-39.avi",
            "video_2026-05-18_06-26-58.avi",
        ],
    )
    host._snapshot_print_baseline()
    assert set(host._print_baseline) == {
        "video_2026-05-18_03-02-39.avi",
        "video_2026-05-18_06-26-58.avi",
    }


def test_record_print_date_preserves_existing(monkeypatch):
    """Recording a new date keeps previously-recorded entries."""
    host = _Host(
        _settings(
            print_dates={"video_2026-05-18_03-02-39.avi": "2026-06-01 10:00"}
        )
    )
    _no_wait(monkeypatch)
    baseline = {"video_2026-05-18_03-02-39.avi": 100}
    host._snapshot_videos = lambda: {
        "video_2026-05-18_03-02-39.avi": 100,
        "video_2026-05-18_06-26-58.avi": 8363434,
    }
    host._record_print_date_worker("2026-06-22 14:42", baseline)
    stored = host._settings.get(["print_dates"])
    assert stored == {
        "video_2026-05-18_03-02-39.avi": "2026-06-01 10:00",
        "video_2026-05-18_06-26-58.avi": "2026-06-22 14:42",
    }
