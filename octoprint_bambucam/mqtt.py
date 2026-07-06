"""MQTT client for controlling and observing the Bambu printer's LED.

Bambu printers run a local MQTT broker on **port 8883** (TLS, self-signed),
user ``bblp`` / LAN access code — the same credentials the FTPS timelapse
download uses. Commands are published to ``device/{serial}/request``; the LED
is toggled with a ``system.ledctrl`` payload, and the printer reports its full
state (including ``lights_report``) on ``device/{serial}/report``.

Two clients live here:

- :class:`BambuMqttClient` — a one-shot connect → publish → disconnect used to
  fire a single command when no monitor is running.
- :class:`BambuMqttMonitor` — a long-lived connection that subscribes to the
  report topic, tracks the real ``chamber_light`` state, and publishes commands
  over the same socket. Started/stopped with the webcam tab.

Neither ever logs the access code. ``serial`` is required (only
OctoPrint-BambuConnector can supply it); without one we fail fast with
``no_serial``.
"""

import json
import ssl
import threading
from typing import Callable, Optional

import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion

MQTT_PORT = 8883
MQTT_USER = "bblp"

CONNECT_TIMEOUT = 15

_RC_NOT_AUTHORISED = 5


# The ledctrl payload, shared by the one-shot client and the monitor.
def _ledctrl_payload(on: bool) -> dict:
    """Build the ``system.ledctrl`` request payload for ``chamber_light``."""
    return {
        "system": {
            "command": "ledctrl",
            "led_node": "chamber_light",
            "led_mode": "on" if on else "off",
            "led_on_time": 500,
            "led_off_time": 500,
            "loop_times": 0,
            "interval_time": 0,
        }
    }


def _light_state_from_report(payload: object) -> Optional[bool]:
    """Extract the ``chamber_light`` on/off state from a report payload.

    Bambu reports lights under ``<group>.lights_report``, a list of
    ``{"node": ..., "mode": "on"/"off"}``. The lights live under different top
    groups across firmware (``system`` or ``print``), so scan both. Returns
    ``True``/``False`` for chamber_light, or ``None`` when the report carries no
    light info (most reports don't — only deltas).
    """
    if not isinstance(payload, dict):
        return None
    for group in payload.values():
        if not isinstance(group, dict):
            continue
        lights = group.get("lights_report")
        if not isinstance(lights, list):
            continue
        for light in lights:
            if isinstance(light, dict) and light.get("node") == "chamber_light":
                mode = light.get("mode")
                if mode in ("on", "off"):
                    return mode == "on"
    return None


def _new_client() -> mqtt.Client:
    """Build a paho 2.x client pinned to the v1 callback API.

    paho 2.0 made ``callback_api_version`` a required argument; ``VERSION1``
    keeps the legacy ``on_connect(client, userdata, flags, rc)`` signature this
    module relies on.
    """
    return mqtt.Client(callback_api_version=CallbackAPIVersion.VERSION1)


class MqttError(Exception):
    """An MQTT operation failed, carrying a classified ``reason``.

    ``reason`` is one of ``no_serial`` / ``unreachable`` / ``auth_failed`` /
    ``timeout`` / ``publish_failed`` so the UI can translate it without parsing
    raw broker errors.
    """

    def __init__(self, reason: str, message: str = ""):
        super().__init__(message or reason)
        self.reason = reason


def _build_ssl_context() -> ssl.SSLContext:
    """SSL context for the printer's self-signed cert.

    Mirrors the FTPS/camera path: TLS floored at 1.2, no cert/hostname
    verification (the printer has no stable hostname and presents a
    self-signed certificate).
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


class BambuMqttClient:
    """One-shot MQTT control client for a single Bambu printer.

    Build it with the printer's host/access_code/serial, then call
    :meth:`set_chamber_light`. Each call opens its own short-lived connection
    (the printer tolerates few concurrent clients), serialized per instance.
    """

    def __init__(
        self,
        logger,
        hostname: str,
        access_code: str,
        serial: str,
        *,
        timeout: int = CONNECT_TIMEOUT,
    ):
        self._logger = logger
        self._hostname = hostname
        self._access_code = access_code
        self._serial = serial
        self._timeout = timeout
        self._lock = threading.Lock()

    def set_chamber_light(self, on: bool) -> None:
        """Turn the printer's LED on or off. Raises :class:`MqttError`."""
        self._publish_request(_ledctrl_payload(on))

    def _publish_request(self, payload: dict) -> None:
        """Connect, publish ``payload`` to the request topic, disconnect."""
        if not self._serial:
            raise MqttError("no_serial", "printer serial is required for MQTT")
        if not self._hostname or not self._access_code:
            raise MqttError("unreachable", "missing host or access code")

        topic = f"device/{self._serial}/request"
        with self._lock:
            connected = threading.Event()
            state: dict[str, int | str | None] = {
                "connect_rc": None,
                "error": None,
            }

            client = _new_client()
            client.username_pw_set(MQTT_USER, self._access_code)
            client.tls_set_context(_build_ssl_context())

            def on_connect(_c, _u, _flags, rc, *_args):
                state["connect_rc"] = int(rc)
                connected.set()

            client.on_connect = on_connect

            try:
                client.connect(self._hostname, MQTT_PORT, keepalive=30)
            except (OSError, ssl.SSLError) as exc:
                raise MqttError("unreachable", str(exc)) from exc

            client.loop_start()
            try:
                if not connected.wait(timeout=self._timeout):
                    raise MqttError("timeout", "broker did not respond")
                rc = state["connect_rc"]
                if rc == _RC_NOT_AUTHORISED:
                    raise MqttError("auth_failed", "bad access code")
                if rc != 0:
                    raise MqttError("unreachable", f"connect rc={rc}")

                info = client.publish(topic, json.dumps(payload), qos=1)
                info.wait_for_publish(timeout=self._timeout)
                if not info.is_published():
                    raise MqttError("publish_failed", "publish not acked")
            finally:
                client.loop_stop()
                try:
                    client.disconnect()
                except Exception:  # noqa: BLE001 - best-effort teardown
                    pass
            self._logger.debug("LED command published to printer")


class BambuMqttMonitor:
    """Long-lived MQTT connection that tracks the printer's light state.

    Subscribes to ``device/{serial}/report``, parses the ``chamber_light``
    state out of each report, and invokes ``on_change(bool)`` whenever it
    changes. Also publishes ``ledctrl`` commands over the same connection
    (:meth:`set_chamber_light`), so only one socket is ever open.

    The connection runs on paho's background loop with automatic reconnect.
    Start it with :meth:`start` and always pair it with :meth:`stop`. The last
    known state is available via :meth:`current_state` (``None`` until the
    first report arrives).
    """

    def __init__(
        self,
        logger,
        hostname: str,
        access_code: str,
        serial: str,
        on_change: Callable[[Optional[bool]], None],
    ):
        self._logger = logger
        self._hostname = hostname
        self._access_code = access_code
        self._serial = serial
        self._on_change = on_change
        self._client: Optional[mqtt.Client] = None
        self._state: Optional[bool] = None
        self._lock = threading.Lock()
        self._req_topic = f"device/{serial}/request"
        self._report_topic = f"device/{serial}/report"

    def current_state(self) -> Optional[bool]:
        """Return the last known chamber-light state, or ``None`` if unknown."""
        with self._lock:
            return self._state

    def start(self) -> None:
        """Connect, subscribe to reports, and request a full state dump.

        Raises :class:`MqttError` if the printer is unreachable, the serial is
        missing, or the access code is rejected.
        """
        if not self._serial:
            raise MqttError("no_serial", "printer serial is required for MQTT")
        if not self._hostname or not self._access_code:
            raise MqttError("unreachable", "missing host or access code")

        connected = threading.Event()
        rc_box: dict = {"rc": None}

        client = _new_client()
        client.username_pw_set(MQTT_USER, self._access_code)
        client.tls_set_context(_build_ssl_context())
        client.reconnect_delay_set(min_delay=1, max_delay=30)

        def on_connect(c, _u, _flags, rc, *_args):
            rc_box["rc"] = int(rc)
            if int(rc) == 0:
                c.subscribe(self._report_topic, qos=0)
                c.publish(
                    self._req_topic,
                    json.dumps({"pushing": {"command": "pushall"}}),
                    qos=0,
                )
            connected.set()

        def on_message(_c, _u, msg):
            self._handle_report(msg.payload)

        client.on_connect = on_connect
        client.on_message = on_message

        try:
            client.connect(self._hostname, MQTT_PORT, keepalive=30)
        except (OSError, ssl.SSLError) as exc:
            raise MqttError("unreachable", str(exc)) from exc

        client.loop_start()
        if not connected.wait(timeout=CONNECT_TIMEOUT):
            self._teardown(client)
            raise MqttError("timeout", "broker did not respond")
        rc = rc_box["rc"]
        if rc == _RC_NOT_AUTHORISED:
            self._teardown(client)
            raise MqttError("auth_failed", "bad access code")
        if rc != 0:
            self._teardown(client)
            raise MqttError("unreachable", f"connect rc={rc}")

        self._client = client
        self._logger.debug("MQTT monitor connected")

    def set_chamber_light(self, on: bool) -> None:
        """Publish a ledctrl command over the live connection."""
        client = self._client
        if client is None:
            raise MqttError("unreachable", "monitor not connected")
        info = client.publish(
            self._req_topic, json.dumps(_ledctrl_payload(on)), qos=1
        )
        info.wait_for_publish(timeout=CONNECT_TIMEOUT)
        if not info.is_published():
            raise MqttError("publish_failed", "publish not acked")

    def stop(self) -> None:
        """Disconnect and tear down the background loop. Idempotent."""
        client, self._client = self._client, None
        if client is not None:
            self._teardown(client)
            self._logger.debug("MQTT monitor stopped")

    def _handle_report(self, raw: bytes) -> None:
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError):
            return
        new_state = _light_state_from_report(payload)
        if new_state is None:
            return
        with self._lock:
            if new_state == self._state:
                return
            self._state = new_state
        try:
            self._on_change(new_state)
        except Exception:  # noqa: BLE001 - callback must never kill the loop
            self._logger.exception("LED state callback failed")

    @staticmethod
    def _teardown(client: mqtt.Client) -> None:
        try:
            client.loop_stop()
        except Exception:  # noqa: BLE001 - best-effort teardown
            pass
        try:
            client.disconnect()
        except Exception:  # noqa: BLE001 - best-effort teardown
            pass
