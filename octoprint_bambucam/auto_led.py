"""Automatic chamber light around a print job (opt-in).

When ``auto_led`` is on, ``PrintStarted`` switches the printer light **on** and
the end of the print (``PrintDone`` / ``PrintFailed`` / ``PrintCancelled``)
switches it **off**, each after its own optional delay in seconds.

Only one switch is ever pending: a new print event cancels the previous timer,
so a print started during a pending off-delay aborts the off (and vice versa).
A paused print is deliberately *not* treated as finished — you often pause
specifically to look inside, and ``_print_active()`` counts a pause as active.

A manual toggle on the live stream wins for the rest of the running print: the
automation then leaves the light alone until the next ``PrintStarted`` resets
the override.

The light is switched off rather than restored to its pre-print state: the real
state is often unknown (``_led_state`` stays ``None`` until the monitor reports,
and the connector read needs BambuConnector active), so a "restore" would
silently degrade to "off" much of the time — worse than a predictable rule.

A duplicate ``PrintStarted`` (observed on the A1 mini, see ``ipcam_sync``) just
re-arms the same on-timer, and setting the light to the state it already has is
a no-op on the printer, so the whole path is idempotent.

``AutoLedMixin`` is mixed into ``BambucamPlugin`` and drives the light through
the plugin's ``_apply_led`` — the same connector → monitor → one-shot-MQTT
fallback chain the manual API toggle uses, sharing its busy flag so the two can
never open two sockets on the printer's fragile broker.
"""

import logging
import threading
from typing import TYPE_CHECKING, Callable, Optional

from octoprint.events import Events

if TYPE_CHECKING:  # pragma: no cover - typing only
    from octoprint.plugin import PluginSettings
    from octoprint.plugin.core import PluginManager

# Events that mean "the print is over". A cancel fires PRINT_CANCELLED *and*
# PRINT_FAILED (octoprint/printer/standard.py fires the latter from a finalize
# thread with reason="cancelled"), so both are listed rather than depending on
# OctoPrint's internal ordering — the re-scheduling below makes the duplicate
# harmless.
_PRINT_END_EVENTS = (
    Events.PRINT_DONE,
    Events.PRINT_FAILED,
    Events.PRINT_CANCELLED,
)

# The manual API toggle holds ``_led_busy`` for the length of one MQTT
# round-trip. An auto switch that collides with a user click waits this out
# instead of dropping the command — unlike a click, there is nobody around to
# press the button again.
_BUSY_RETRY_SECONDS = 2
_BUSY_MAX_RETRIES = 10


class AutoLedMixin:
    """Switch the printer light on/off around a print job."""

    _settings: "PluginSettings"
    _plugin_manager: "PluginManager"
    _logger: logging.Logger
    _identifier: str

    # Provided by BambucamPlugin / sibling mixins; declared callable-typed so
    # the type checker sees them without shadowing the real implementations.
    _apply_led: "Callable[[bool], Optional[str]]"
    _current_led_state: "Callable[[], Optional[bool]]"
    _print_active: "Callable[[], bool]"
    _led_lock: threading.Lock
    _led_busy: bool

    def _init_auto_led(self) -> None:
        """Initialize auto-LED state. Call from the plugin ``__init__``."""
        self._auto_led_lock = threading.Lock()
        self._auto_led_cancel: Optional[threading.Event] = None
        # Set by a manual toggle during a print, cleared at PrintStarted: while
        # True the automation keeps its hands off the light.
        self._auto_led_overridden = False

    def on_auto_led_event(self, event, _payload) -> None:
        """Arm the on/off switch when a print starts or ends.

        Returns immediately — the delay and the MQTT round-trip both happen on
        a daemon thread, so OctoPrint's event thread is never blocked.
        """
        if not self._settings.get_boolean(["auto_led"]):
            return
        if event == Events.PRINT_STARTED:
            # A new print clears a manual override from the previous one.
            self._auto_led_overridden = False
            self._schedule_auto_led(True, ["auto_led_on_delay"])
        elif event in _PRINT_END_EVENTS:
            if self._auto_led_overridden:
                self._logger.info(
                    "auto-LED: %s, but the light was toggled by hand "
                    "- leaving it as it is",
                    event,
                )
                return
            self._logger.debug("auto-LED: %s, arming the switch off", event)
            self._schedule_auto_led(False, ["auto_led_off_delay"])

    def _note_manual_led(self) -> None:
        """Record that the user toggled the light by hand.

        Called from the ``set_led`` API handler. Only a click *during* a print
        takes precedence — a click while idle must not affect the next print.
        Also drops a pending auto switch so an on-delay cannot run against the
        state the user just chose. Runs in the request thread, so it stays
        cheap and never raises.
        """
        try:
            if not self._settings.get_boolean(["auto_led"]):
                return
            if not self._print_active():
                return
            self._auto_led_overridden = True
            self._cancel_auto_led()
            self._logger.debug(
                "auto-LED: manual toggle during a print takes precedence"
            )
        # a UI click must never fail because the automation bookkeeping did
        except Exception:  # noqa: BLE001  # pylint: disable=broad-except
            self._logger.exception("could not record manual LED toggle")

    def _schedule_auto_led(self, on: bool, delay_key) -> None:
        """Cancel any pending switch and arm a new one after its delay."""
        delay = max(0, self._settings.get_int(delay_key) or 0)
        cancel = threading.Event()
        with self._auto_led_lock:
            if self._auto_led_cancel is not None:
                self._auto_led_cancel.set()
            self._auto_led_cancel = cancel
        threading.Thread(
            target=self._auto_led_worker,
            args=(on, delay, cancel),
            daemon=True,
        ).start()

    def _cancel_auto_led(self) -> None:
        """Drop a pending switch. Idempotent; also called on shutdown."""
        with self._auto_led_lock:
            if self._auto_led_cancel is not None:
                self._auto_led_cancel.set()
                self._auto_led_cancel = None

    def _auto_led_worker(
        self, on: bool, delay: int, cancel: threading.Event
    ) -> None:
        """Wait out the delay, then drive the light (unless superseded)."""
        try:
            self._logger.debug(
                "auto-LED: switch %s armed, waiting %ss",
                "on" if on else "off",
                delay,
            )
            if cancel.wait(timeout=delay):
                self._logger.debug(
                    "auto-LED: switch %s superseded during its delay",
                    "on" if on else "off",
                )
                return
            if not self._claim_led(cancel):
                return
            try:
                # The user may have grabbed the light while we were waiting.
                if not on and self._auto_led_overridden:
                    self._logger.debug(
                        "auto-LED: light was toggled by hand, "
                        "skipping the switch off"
                    )
                    return
                if not self._auto_led_wanted(on):
                    return
                reason = self._apply_led(on)
            finally:
                with self._led_lock:
                    self._led_busy = False
            if reason is not None:
                self._logger.warning(
                    "auto-LED: could not switch the light %s (%s)",
                    "on" if on else "off",
                    reason,
                )
                return
            self._logger.info(
                "auto-LED: light switched %s", "on" if on else "off"
            )
            self._push_auto_led_state(on)
        # a background trigger must never crash the event thread
        except (OSError, RuntimeError, ValueError):
            self._logger.exception("auto-LED failed")
        finally:
            with self._auto_led_lock:
                if self._auto_led_cancel is cancel:
                    self._auto_led_cancel = None

    def _auto_led_wanted(self, on: bool) -> bool:
        """Whether the pending switch should still go through.

        Only the switch **off** is guarded. Someone who turned the light off at
        the printer's own display never reaches ``set_led``, so the override
        flag stays unset — the real state is the only evidence we have. When it
        already reads off, the automation leaves it alone, so an operator's
        choice is never undone a second time.

        The switch on is unconditional: a light that is already on makes the
        command a no-op on the printer anyway, and skipping it would turn a
        stale read into a missed switch.

        An unknown state (``None``) means BambuConnector is not the active
        connection. It is not evidence of anything, so the switch proceeds.
        """
        if on:
            return True
        state = self._current_led_state()
        if state is False:
            self._logger.info(
                "auto-LED: light is already off (switched at the printer?), "
                "leaving it alone"
            )
            return False
        return True

    def _claim_led(self, cancel: threading.Event) -> bool:
        """Take the shared LED busy flag, waiting out a manual command.

        Returns False when cancelled or when the light stayed busy — the
        printer's broker tolerates very few connections, so waiting is the only
        safe option; opening a second socket is not.
        """
        for _ in range(_BUSY_MAX_RETRIES):
            with self._led_lock:
                if not self._led_busy:
                    self._led_busy = True
                    return True
            if cancel.wait(timeout=_BUSY_RETRY_SECONDS):
                return False
        self._logger.warning("auto-LED: light stayed busy, skipping the switch")
        return False

    def _push_auto_led_state(self, on: bool) -> None:
        """Mirror an auto switch to the webcam button. Never raises.

        Reuses the monitor's ``led_state`` message, so the frontend needs no
        change: its handler already ignores a push that would stomp a click
        still in flight.
        """
        try:
            self._plugin_manager.send_plugin_message(
                self._identifier, {"type": "led_state", "on": on}
            )
        # never let a push kill the worker
        except Exception:  # noqa: BLE001  # pylint: disable=broad-except
            self._logger.exception("could not push auto-LED state")
