"""Tests for the automatic chamber light around a print (AutoLedMixin)."""

# pylint: disable=protected-access,redefined-outer-name,too-few-public-methods
# pylint: disable=attribute-defined-outside-init

import logging
import threading
import time

from octoprint.events import Events

from octoprint_bambucam.auto_led import AutoLedMixin


class _FakeSettings:
    """Minimal in-memory stand-in for OctoPrint's ``PluginSettings``."""

    def __init__(self, values):
        self._v = dict(values)

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


class _Host(AutoLedMixin):
    """Concrete AutoLedMixin with the deps BambucamPlugin would provide."""

    def __init__(self, settings_values, *, apply_result=None, printing=False):
        settings = _FakeSettings(settings_values)
        self._settings = settings  # type: ignore[assignment]
        self._logger = logging.getLogger("test.auto_led")
        self._identifier = "bambucam"
        self._led_lock = threading.Lock()
        self._led_busy = False
        self._printing = printing
        self.applied = []  # recorded _apply_led(on) calls
        self.apply_result = apply_result  # None = success, else a reason
        self.apply_error = None  # exception to raise instead
        self.sent = []  # pushed plugin messages
        self._init_auto_led()

    class _PM:
        """Plugin-manager stand-in that records sent plugin messages."""

        def __init__(self, outer):
            self._outer = outer

        def send_plugin_message(self, _ident, msg):
            """Record ``msg``; the identifier is irrelevant to the tests."""
            self._outer.sent.append(msg)

    @property
    def _plugin_manager(self):  # type: ignore[override]
        """Return a recording plugin-manager stand-in."""
        return self._PM(self)

    # dep from TimelapseOpsMixin
    def _print_active(self):
        """Whether a print is currently active."""
        return self._printing

    # dep from BambucamPlugin
    def _apply_led(self, on):  # type: ignore[override]
        """Record the switch instead of talking to the printer."""
        self.applied.append(on)
        if self.apply_error is not None:
            raise self.apply_error
        return self.apply_result


def _settings(**over):
    """Build a default auto-LED settings dict, overridable via kwargs."""
    base = {
        "auto_led": True,
        "auto_led_on_delay": 0,
        "auto_led_off_delay": 0,
    }
    base.update(over)
    return base


def _await(predicate, timeout=2.0):
    """Poll ``predicate`` until true or the timeout elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


# ── gating ────────────────────────────────────────────────────────────────


def test_disabled_does_nothing():
    """With auto-LED off, PRINT_STARTED schedules nothing."""
    host = _Host(_settings(auto_led=False))
    host.on_auto_led_event(Events.PRINT_STARTED, {})
    assert host._auto_led_cancel is None
    assert not host.applied


def test_paused_and_resumed_are_ignored():
    """A pause is not the end of a print, so the light stays as it is."""
    host = _Host(_settings())
    host.on_auto_led_event(Events.PRINT_PAUSED, {})
    host.on_auto_led_event(Events.PRINT_RESUMED, {})
    assert not _await(lambda: host.applied, timeout=0.2)


# ── switching ─────────────────────────────────────────────────────────────


def test_print_started_switches_on():
    """PRINT_STARTED turns the light on."""
    host = _Host(_settings())
    host.on_auto_led_event(Events.PRINT_STARTED, {})
    assert _await(lambda: host.applied == [True])


def test_print_done_switches_off():
    """PRINT_DONE turns the light off."""
    host = _Host(_settings())
    host.on_auto_led_event(Events.PRINT_DONE, {})
    assert _await(lambda: host.applied == [False])


def test_print_failed_switches_off():
    """A failed print turns the light off."""
    host = _Host(_settings())
    host.on_auto_led_event(Events.PRINT_FAILED, {})
    assert _await(lambda: host.applied == [False])


def test_print_cancelled_switches_off():
    """A cancelled print turns the light off."""
    host = _Host(_settings())
    host.on_auto_led_event(Events.PRINT_CANCELLED, {})
    assert _await(lambda: host.applied == [False])


def test_cancel_fires_both_events_and_only_switches_off():
    """OctoPrint fires PRINT_CANCELLED *and* PRINT_FAILED; both mean off."""
    host = _Host(_settings())
    host.on_auto_led_event(Events.PRINT_CANCELLED, {})
    host.on_auto_led_event(Events.PRINT_FAILED, {})
    assert _await(lambda: host.applied)
    assert all(on is False for on in host.applied)


def test_duplicate_print_started_is_idempotent():
    """The A1 mini's duplicate PRINT_STARTED only ever switches on."""
    host = _Host(_settings())
    host.on_auto_led_event(Events.PRINT_STARTED, {})
    host.on_auto_led_event(Events.PRINT_STARTED, {})
    assert _await(lambda: host.applied)
    assert all(on is True for on in host.applied)


# ── delays and cancellation ───────────────────────────────────────────────


def test_on_delay_is_waited():
    """A configured on-delay keeps the switch pending."""
    host = _Host(_settings(auto_led_on_delay=30))
    host.on_auto_led_event(Events.PRINT_STARTED, {})
    assert host._auto_led_cancel is not None
    assert not _await(lambda: host.applied, timeout=0.2)
    host._cancel_auto_led()


def test_print_started_cancels_pending_off():
    """A new print aborts a still-pending switch-off."""
    host = _Host(_settings(auto_led_off_delay=30))
    host.on_auto_led_event(Events.PRINT_DONE, {})
    assert host._auto_led_cancel is not None
    host.on_auto_led_event(Events.PRINT_STARTED, {})
    assert _await(lambda: host.applied == [True])
    assert False not in host.applied


def test_event_handler_does_not_block():
    """Scheduling returns promptly even with a long delay configured."""
    host = _Host(_settings(auto_led_on_delay=600))
    start = time.monotonic()
    host.on_auto_led_event(Events.PRINT_STARTED, {})
    assert time.monotonic() - start < 0.5
    host._cancel_auto_led()


def test_cancel_auto_led_is_idempotent():
    """Cancelling twice is harmless."""
    host = _Host(_settings(auto_led_on_delay=30))
    host.on_auto_led_event(Events.PRINT_STARTED, {})
    host._cancel_auto_led()
    host._cancel_auto_led()
    assert host._auto_led_cancel is None


# ── frontend push ─────────────────────────────────────────────────────────


def test_push_on_success():
    """A successful switch is mirrored to the webcam button."""
    host = _Host(_settings())
    host.on_auto_led_event(Events.PRINT_STARTED, {})
    assert _await(lambda: host.sent)
    assert host.sent == [{"type": "led_state", "on": True}]


def test_no_push_on_failure():
    """An unreachable printer logs but pushes nothing."""
    host = _Host(_settings(), apply_result="unreachable")
    host.on_auto_led_event(Events.PRINT_STARTED, {})
    assert _await(lambda: host.applied == [True])
    assert not _await(lambda: host.sent, timeout=0.2)


def test_push_failure_is_contained(monkeypatch):
    """A failing push never kills the worker."""
    host = _Host(_settings())

    def boom(*_a, **_k):
        raise RuntimeError("socket gone")

    monkeypatch.setattr(host._PM, "send_plugin_message", boom)
    host.on_auto_led_event(Events.PRINT_STARTED, {})
    assert _await(lambda: host.applied == [True])
    assert _await(lambda: host._auto_led_cancel is None)


# ── busy flag sharing ─────────────────────────────────────────────────────


def test_busy_is_waited_out():
    """An auto switch waits for an in-flight manual command."""
    host = _Host(_settings())
    host._led_busy = True
    host.on_auto_led_event(Events.PRINT_STARTED, {})
    assert not _await(lambda: host.applied, timeout=0.2)
    host._led_busy = False
    assert _await(lambda: host.applied == [True], timeout=5)


def test_busy_flag_released_after_switch():
    """The shared busy flag is handed back when the switch is done."""
    host = _Host(_settings())
    host.on_auto_led_event(Events.PRINT_STARTED, {})
    assert _await(lambda: host.applied == [True])
    assert _await(lambda: host._led_busy is False)


def test_claim_led_gives_up_when_cancelled():
    """Claiming the light returns False once the switch is cancelled."""
    host = _Host(_settings())
    host._led_busy = True
    cancel = threading.Event()
    cancel.set()
    assert host._claim_led(cancel) is False


def test_claim_led_gives_up_when_permanently_busy(monkeypatch):
    """A light that never frees up skips the switch instead of hanging."""
    monkeypatch.setattr("octoprint_bambucam.auto_led._BUSY_MAX_RETRIES", 2)
    monkeypatch.setattr("octoprint_bambucam.auto_led._BUSY_RETRY_SECONDS", 0.01)
    host = _Host(_settings())
    host._led_busy = True
    assert host._claim_led(threading.Event()) is False
    assert not host.applied


# ── manual override ───────────────────────────────────────────────────────


def test_manual_toggle_during_print_suppresses_switch_off():
    """A hand toggle during a print keeps the automation off the light."""
    host = _Host(_settings(), printing=True)
    host._note_manual_led()
    assert host._auto_led_overridden is True
    host.on_auto_led_event(Events.PRINT_DONE, {})
    assert not _await(lambda: host.applied, timeout=0.2)


def test_manual_toggle_cancels_pending_switch():
    """A hand toggle drops a pending auto switch for this print."""
    host = _Host(_settings(auto_led_on_delay=30), printing=True)
    host.on_auto_led_event(Events.PRINT_STARTED, {})
    assert host._auto_led_cancel is not None
    host._note_manual_led()
    assert host._auto_led_cancel is None
    assert not _await(lambda: host.applied, timeout=0.2)


def test_next_print_resets_the_override():
    """PRINT_STARTED clears a manual override from the previous print."""
    host = _Host(_settings(), printing=True)
    host._note_manual_led()
    host.on_auto_led_event(Events.PRINT_STARTED, {})
    assert host._auto_led_overridden is False
    assert _await(lambda: host.applied == [True])


def test_manual_toggle_while_idle_does_not_override():
    """A click while idle must not affect the next print."""
    host = _Host(_settings(), printing=False)
    host._note_manual_led()
    assert host._auto_led_overridden is False
    host.on_auto_led_event(Events.PRINT_DONE, {})
    assert _await(lambda: host.applied == [False])


def test_manual_toggle_ignored_when_automation_off():
    """With auto-LED disabled the override flag is never set."""
    host = _Host(_settings(auto_led=False), printing=True)
    host._note_manual_led()
    assert host._auto_led_overridden is False


def test_manual_toggle_survives_a_broken_settings_read(monkeypatch):
    """A UI click never fails because the bookkeeping did."""
    host = _Host(_settings(), printing=True)

    def boom(_path):
        raise RuntimeError("settings gone")

    monkeypatch.setattr(host._settings, "get_boolean", boom)
    host._note_manual_led()  # must not raise
    assert host._auto_led_overridden is False


def test_override_during_pending_off_delay_aborts_the_switch():
    """An override set after the delay started still stops the switch.

    Drives the worker directly with an already-elapsed delay so the second
    override check — the one after the wait — is the thing under test.
    """
    host = _Host(_settings(), printing=True)
    host._auto_led_overridden = True
    host._auto_led_worker(False, 0, threading.Event())
    assert not host.applied
    assert host._led_busy is False


def test_override_does_not_block_switch_on():
    """An override only suppresses the switch off, not a new print's on."""
    host = _Host(_settings(), printing=True)
    host._auto_led_overridden = True
    host._schedule_auto_led(True, ["auto_led_on_delay"])
    assert _await(lambda: host.applied == [True])


# ── error containment ─────────────────────────────────────────────────────


def test_apply_led_exception_is_contained():
    """A raising transport never crashes the worker thread."""
    host = _Host(_settings())
    host.apply_error = OSError("broker gone")
    host.on_auto_led_event(Events.PRINT_STARTED, {})
    assert _await(lambda: host.applied == [True])
    assert _await(lambda: host._auto_led_cancel is None)
    assert not host.sent
    assert host._led_busy is False
