"""Tests for octoprint_bambucam.mqtt.BambuMqttClient."""

# pylint: disable=protected-access,redefined-outer-name,too-few-public-methods
# pylint: disable=missing-function-docstring,missing-class-docstring
# pylint: disable=unused-argument

import json
import logging
from typing import Any, Callable, Optional, cast

import pytest

from octoprint_bambucam import mqtt as mqtt_mod
from octoprint_bambucam.mqtt import (
    BambuMqttClient,
    BambuMqttMonitor,
    MqttError,
    _light_state_from_report,
)


@pytest.fixture()
def logger():
    return logging.getLogger("test.mqtt")


class FakePublishInfo:
    def __init__(self, published=True):
        self._published = published

    def wait_for_publish(self, timeout=None):
        return None

    def is_published(self):
        return self._published


class FakeClient:
    """A fake paho client that drives on_connect synchronously on connect()."""

    def __init__(self, *, connect_rc=0, connect_raises=None, published=True):
        self._connect_rc = connect_rc
        self._connect_raises = connect_raises
        self._published = published
        self.on_connect = None
        self.username = None
        self.password = None
        self.tls_set = False
        self.published_topic: str | None = None
        self.published_payload: str = ""
        self._host: str | None = None
        self._port: int | None = None

    def username_pw_set(self, user, password):
        self.username = user
        self.password = password

    def tls_set_context(self, ctx):
        self.tls_set = True

    def connect(self, host, port, keepalive=60):
        if self._connect_raises is not None:
            raise self._connect_raises
        self._host = host
        self._port = port

    def loop_start(self):
        # fire the connect callback as the broker would
        if self.on_connect is not None:
            self.on_connect(self, None, {}, self._connect_rc)

    def loop_stop(self):
        pass

    def publish(self, topic, payload, qos=0):
        self.published_topic = topic
        self.published_payload = payload
        return FakePublishInfo(self._published)

    def disconnect(self):
        pass


def _patch_client(monkeypatch, client):
    monkeypatch.setattr(mqtt_mod, "_new_client", lambda: client)
    return client


def _make(logger, **kw):
    return BambuMqttClient(
        logger,
        kw.pop("host", "192.168.1.50"),
        kw.pop("code", "12345678"),
        kw.pop("serial", "SERIAL123"),
        timeout=1,
    )


class TestSetChamberLight:
    def test_on_publishes_correct_payload(self, logger, monkeypatch):
        client = _patch_client(monkeypatch, FakeClient())
        _make(logger).set_chamber_light(True)
        assert client.published_topic == "device/SERIAL123/request"
        payload = json.loads(client.published_payload)
        assert payload["system"]["command"] == "ledctrl"
        assert payload["system"]["led_node"] == "chamber_light"
        assert payload["system"]["led_mode"] == "on"

    def test_off_sets_mode_off(self, logger, monkeypatch):
        client = _patch_client(monkeypatch, FakeClient())
        _make(logger).set_chamber_light(False)
        payload = json.loads(client.published_payload)
        assert payload["system"]["led_mode"] == "off"

    def test_uses_bblp_user_and_access_code(self, logger, monkeypatch):
        client = _patch_client(monkeypatch, FakeClient())
        _make(logger, code="secretcode").set_chamber_light(True)
        assert client.username == "bblp"
        assert client.password == "secretcode"
        assert client.tls_set is True


class TestErrors:
    def test_no_serial_fails_fast(self, logger, monkeypatch):
        # _new_client must never be called when the serial is missing
        client = _patch_client(monkeypatch, FakeClient())
        svc = BambuMqttClient(logger, "h", "c", "", timeout=1)
        with pytest.raises(MqttError) as exc:
            svc.set_chamber_light(True)
        assert exc.value.reason == "no_serial"
        assert client.published_topic is None

    def test_missing_host_unreachable(self, logger, monkeypatch):
        _patch_client(monkeypatch, FakeClient())
        svc = BambuMqttClient(logger, "", "c", "S", timeout=1)
        with pytest.raises(MqttError) as exc:
            svc.set_chamber_light(True)
        assert exc.value.reason == "unreachable"

    def test_connect_oserror_unreachable(self, logger, monkeypatch):
        _patch_client(
            monkeypatch, FakeClient(connect_raises=OSError("no route"))
        )
        with pytest.raises(MqttError) as exc:
            _make(logger).set_chamber_light(True)
        assert exc.value.reason == "unreachable"

    def test_bad_access_code_auth_failed(self, logger, monkeypatch):
        _patch_client(monkeypatch, FakeClient(connect_rc=5))
        with pytest.raises(MqttError) as exc:
            _make(logger).set_chamber_light(True)
        assert exc.value.reason == "auth_failed"

    def test_other_rc_unreachable(self, logger, monkeypatch):
        _patch_client(monkeypatch, FakeClient(connect_rc=3))
        with pytest.raises(MqttError) as exc:
            _make(logger).set_chamber_light(True)
        assert exc.value.reason == "unreachable"

    def test_publish_not_acked(self, logger, monkeypatch):
        _patch_client(monkeypatch, FakeClient(published=False))
        with pytest.raises(MqttError) as exc:
            _make(logger).set_chamber_light(True)
        assert exc.value.reason == "publish_failed"

    def test_timeout_when_connect_never_fires(self, logger, monkeypatch):
        class SilentClient(FakeClient):
            def loop_start(self):
                pass  # never fire on_connect → wait() times out

        _patch_client(monkeypatch, SilentClient())
        with pytest.raises(MqttError) as exc:
            _make(logger).set_chamber_light(True)
        assert exc.value.reason == "timeout"


class TestNoAccessCodeLeak:
    def test_access_code_never_logged(self, logger, monkeypatch, caplog):
        _patch_client(monkeypatch, FakeClient())
        with caplog.at_level(logging.DEBUG, logger="test.mqtt"):
            _make(logger, code="topsecret").set_chamber_light(True)
        assert "topsecret" not in caplog.text


# ---------------------------------------------------------------------------
# report parsing
# ---------------------------------------------------------------------------


class TestLightStateFromReport:
    def test_on_under_system_group(self):
        report = {
            "system": {
                "lights_report": [{"node": "chamber_light", "mode": "on"}]
            }
        }
        assert _light_state_from_report(report) is True

    def test_off_under_print_group(self):
        report = {
            "print": {
                "lights_report": [
                    {"node": "work_light", "mode": "on"},
                    {"node": "chamber_light", "mode": "off"},
                ]
            }
        }
        assert _light_state_from_report(report) is False

    def test_no_light_info_returns_none(self):
        assert (
            _light_state_from_report({"print": {"gcode_state": "IDLE"}}) is None
        )

    def test_non_dict_returns_none(self):
        assert _light_state_from_report("nope") is None


# ---------------------------------------------------------------------------
# standing monitor
# ---------------------------------------------------------------------------


class MonitorFakeClient(FakeClient):
    """FakeClient with subscribe/reconnect + on_message support."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.on_message: Optional[Callable[..., Any]] = None
        self.subscribed = []
        self.requests = []

    def reconnect_delay_set(self, min_delay=1, max_delay=120):
        pass

    def subscribe(self, topic, qos=0):
        self.subscribed.append(topic)

    def publish(self, topic, payload, qos=0):
        self.requests.append((topic, payload))
        return FakePublishInfo(self._published)

    def feed_report(self, payload):
        """Simulate the broker delivering a report message."""
        msg = type("Msg", (), {"payload": json.dumps(payload).encode()})()
        cb = self.on_message
        assert cb is not None
        cb(self, None, msg)  # pylint: disable=not-callable


def _monitor(logger, changes, **kw):
    return BambuMqttMonitor(
        logger,
        kw.pop("host", "10.0.0.5"),
        kw.pop("code", "code"),
        kw.pop("serial", "SER1"),
        on_change=changes.append,
    )


class TestMonitor:
    def test_start_subscribes_and_requests_pushall(self, logger, monkeypatch):
        client = _patch_client(monkeypatch, MonitorFakeClient())
        mon = _monitor(logger, [])
        mon.start()
        assert "device/SER1/report" in client.subscribed
        # a pushall request was sent so the printer dumps its full state
        assert any("pushall" in p for _t, p in client.requests)

    def test_report_updates_state_and_fires_callback(self, logger, monkeypatch):
        client = _patch_client(monkeypatch, MonitorFakeClient())
        changes = []
        mon = _monitor(logger, changes)
        mon.start()
        client.feed_report(
            {
                "system": {
                    "lights_report": [{"node": "chamber_light", "mode": "on"}]
                }
            }
        )
        assert mon.current_state() is True
        assert changes == [True]

    def test_unchanged_state_does_not_refire(self, logger, monkeypatch):
        client = _patch_client(monkeypatch, MonitorFakeClient())
        changes = []
        mon = _monitor(logger, changes)
        mon.start()
        on = {
            "system": {
                "lights_report": [{"node": "chamber_light", "mode": "on"}]
            }
        }
        client.feed_report(on)
        client.feed_report(on)  # same → no second callback
        assert changes == [True]

    def test_report_without_lights_ignored(self, logger, monkeypatch):
        client = _patch_client(monkeypatch, MonitorFakeClient())
        changes = []
        mon = _monitor(logger, changes)
        mon.start()
        client.feed_report({"print": {"gcode_state": "RUNNING"}})
        assert mon.current_state() is None
        assert not changes

    def test_set_chamber_light_publishes_over_connection(
        self, logger, monkeypatch
    ):
        client = _patch_client(monkeypatch, MonitorFakeClient())
        mon = _monitor(logger, [])
        mon.start()
        mon.set_chamber_light(True)
        ledctrl = [p for _t, p in client.requests if "ledctrl" in p]
        assert ledctrl and json.loads(ledctrl[-1])["system"]["led_mode"] == "on"

    def test_set_before_start_raises(self, logger, monkeypatch):
        _patch_client(monkeypatch, MonitorFakeClient())
        mon = _monitor(logger, [])
        with pytest.raises(MqttError) as exc:
            mon.set_chamber_light(True)
        assert exc.value.reason == "unreachable"

    def test_no_serial_fails_fast(self, logger, monkeypatch):
        _patch_client(monkeypatch, MonitorFakeClient())
        mon = BambuMqttMonitor(logger, "h", "c", "", on_change=lambda _s: None)
        with pytest.raises(MqttError) as exc:
            mon.start()
        assert exc.value.reason == "no_serial"

    def test_auth_failure_on_start(self, logger, monkeypatch):
        _patch_client(monkeypatch, MonitorFakeClient(connect_rc=5))
        mon = _monitor(logger, [])
        with pytest.raises(MqttError) as exc:
            mon.start()
        assert exc.value.reason == "auth_failed"

    def test_stop_is_idempotent(self, logger, monkeypatch):
        _patch_client(monkeypatch, MonitorFakeClient())
        mon = _monitor(logger, [])
        mon.start()
        mon.stop()
        mon.stop()  # second call must not raise

    def test_connect_oserror_unreachable(self, logger, monkeypatch):
        _patch_client(
            monkeypatch, MonitorFakeClient(connect_raises=OSError("down"))
        )
        mon = _monitor(logger, [])
        with pytest.raises(MqttError) as exc:
            mon.start()
        assert exc.value.reason == "unreachable"

    def test_start_timeout_when_connect_silent(self, logger, monkeypatch):
        class SilentMonitor(MonitorFakeClient):
            def loop_start(self):
                pass  # never fire on_connect

        _patch_client(monkeypatch, SilentMonitor())
        monkeypatch.setattr(mqtt_mod, "CONNECT_TIMEOUT", 0.1)
        mon = _monitor(logger, [])
        with pytest.raises(MqttError) as exc:
            mon.start()
        assert exc.value.reason == "timeout"

    def test_bad_json_report_ignored(self, logger, monkeypatch):
        client = _patch_client(monkeypatch, MonitorFakeClient())
        changes = []
        mon = _monitor(logger, changes)
        mon.start()
        msg = type("Msg", (), {"payload": b"{not json"})()
        cb = client.on_message
        assert cb is not None
        cb(client, None, msg)  # must not raise  # pylint: disable=not-callable
        assert not changes

    def test_callback_exception_does_not_propagate(self, logger, monkeypatch):
        client = _patch_client(monkeypatch, MonitorFakeClient())

        def boom(_state):
            raise RuntimeError("callback blew up")

        mon = BambuMqttMonitor(logger, "h", "c", "SER1", on_change=boom)
        mon.start()
        # the loop must survive a throwing callback
        client.feed_report(
            {
                "system": {
                    "lights_report": [{"node": "chamber_light", "mode": "on"}]
                }
            }
        )
        assert mon.current_state() is True

    def test_teardown_swallows_errors(self, logger):
        class AngryClient:
            def loop_stop(self):
                raise RuntimeError("no")

            def disconnect(self):
                raise RuntimeError("no")

        BambuMqttMonitor._teardown(cast(Any, AngryClient()))  # must not raise
