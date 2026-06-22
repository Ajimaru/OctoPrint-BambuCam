"""Tests for octoprint_bambucam.connector_led (BambuConnector light path)."""

# pylint: disable=protected-access,too-few-public-methods

from typing import Any

from octoprint_bambucam import connector_led


class FakeBpmPrinter:
    """Stand-in for bpm.BambuPrinter: a light_state property over a backing."""

    def __init__(self, *, state="on", raise_on_set=False, raise_on_get=False):
        self._state = state
        self._raise_set = raise_on_set
        self._raise_get = raise_on_get
        self.set_calls = []

    @property
    def light_state(self):
        """Return True when the light is on (may raise if configured)."""
        if self._raise_get:
            raise RuntimeError("get boom")
        return self._state == "on"

    @light_state.setter
    def light_state(self, value):
        """Record the requested light state (may raise if configured)."""
        if self._raise_set:
            raise RuntimeError("set boom")
        self.set_calls.append(bool(value))
        self._state = "on" if value else "off"


class NoLightClient:
    """A client with no light_state attribute (wrong API)."""


class FakeConnection:
    """Stand-in for a printer connection holding a connector name and client."""

    def __init__(self, *, connector="bambu", client: Any = None):
        self.connector = connector
        self._client: Any = client


class FakePrinter:
    """Stand-in for the OctoPrint printer exposing a private _connection."""

    def __init__(self, connection):
        self._connection = connection


def _printer(**kw):
    """Build a FakePrinter wrapping a FakeBpmPrinter client."""
    return FakePrinter(FakeConnection(client=FakeBpmPrinter(**kw)))


class TestAvailable:
    """Tests for connector_led.available()."""

    def test_available_with_light_client(self):
        """available() is True for a Bambu connection with a light client."""
        assert connector_led.available(_printer()) is True

    def test_not_available_wrong_connector(self):
        """available() is False when the connector is not Bambu."""
        p = FakePrinter(
            FakeConnection(connector="virtual", client=FakeBpmPrinter())
        )
        assert connector_led.available(p) is False

    def test_not_available_no_connection(self):
        """available() is False when there is no connection."""
        assert connector_led.available(FakePrinter(None)) is False

    def test_not_available_client_without_light_state(self):
        """available() is False when the client lacks light_state."""
        p = FakePrinter(FakeConnection(client=NoLightClient()))
        assert connector_led.available(p) is False

    def test_missing_attr_is_safe(self):
        """available() is False (no raise) for an object lacking _connection."""
        assert connector_led.available(object()) is False

    def test_raising_getattr_is_safe(self):
        """A raising _connection getter is swallowed by all entry points."""

        class Angry:
            """Printer whose _connection access always raises."""

            @property
            def _connection(self):
                raise RuntimeError("boom")

        a = Angry()
        assert connector_led.available(a) is False
        assert connector_led.set_chamber_light(a, True) is False
        assert connector_led.current_state(a) is None


class TestPublicConnectionApi:
    """Tests for the public current_connection() lookup path."""

    def test_uses_current_connection_property(self):
        """available() honours a public current_connection() method."""
        conn = FakeConnection(client=FakeBpmPrinter())

        class PublicPrinter:
            """Printer exposing only the public current_connection()."""

            def current_connection(self):
                """Return the prepared connection."""
                return conn

        assert connector_led.available(PublicPrinter()) is True

    def test_current_connection_raising_falls_back(self):
        """A raising current_connection() falls back to _connection."""
        conn = FakeConnection(client=FakeBpmPrinter())

        class Mixed:
            """Printer with a raising public API but a usable _connection."""

            _connection = conn

            def current_connection(self):
                """Raise to force the fallback path."""
                raise RuntimeError("boom")

        assert connector_led.available(Mixed()) is True


class TestDebugLogging:
    """Tests for debug-logging resilience."""

    def test_dbg_swallows_logger_errors(self):
        """A logger that raises does not break available()."""

        class AngryLogger:
            """Logger whose debug() always raises."""

            def debug(self, *_a, **_k):
                """Raise to simulate a broken logger."""
                raise RuntimeError("log boom")

        p = FakePrinter(FakeConnection(connector="virtual"))
        assert connector_led.available(p, AngryLogger()) is False


class TestSetChamberLight:
    """Tests for connector_led.set_chamber_light()."""

    def test_sets_on(self):
        """Turning the light on succeeds and records the call."""
        p = _printer(state="off")
        assert connector_led.set_chamber_light(p, True) is True
        assert p._connection._client.set_calls == [True]

    def test_sets_off(self):
        """Turning the light off succeeds and records the call."""
        p = _printer(state="on")
        assert connector_led.set_chamber_light(p, False) is True
        assert p._connection._client.set_calls == [False]

    def test_returns_false_when_unavailable(self):
        """set_chamber_light() is False when no light client is available."""
        p = FakePrinter(FakeConnection(connector="virtual"))
        assert connector_led.set_chamber_light(p, True) is False

    def test_set_error_returns_false(self):
        """A raising setter makes set_chamber_light() return False."""
        p = FakePrinter(
            FakeConnection(client=FakeBpmPrinter(raise_on_set=True))
        )
        assert connector_led.set_chamber_light(p, True) is False


class TestCurrentState:
    """Tests for connector_led.current_state()."""

    def test_on(self):
        """current_state() is True when the light is on."""
        assert connector_led.current_state(_printer(state="on")) is True

    def test_off(self):
        """current_state() is False when the light is off."""
        assert connector_led.current_state(_printer(state="off")) is False

    def test_get_error_is_none(self):
        """A raising getter makes current_state() return None."""
        p = FakePrinter(
            FakeConnection(client=FakeBpmPrinter(raise_on_get=True))
        )
        assert connector_led.current_state(p) is None

    def test_unavailable_is_none(self):
        """current_state() is None when no light client is available."""
        p = FakePrinter(FakeConnection(connector="virtual"))
        assert connector_led.current_state(p) is None
