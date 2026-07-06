"""Drive the printer light through OctoPrint-BambuConnector's connection.

The Bambu A1 mini's local MQTT broker tolerates only ~1-2 connections, and
OctoPrint-BambuConnector keeps one open permanently to drive the printer.
Opening a *second* connection from BambuCam therefore fails intermittently with
a TLS handshake timeout — which is what the standalone
:mod:`octoprint_bambucam.mqtt` client hits.

When BambuConnector is the active connection we instead drive the light over its
already-open connection: it wraps the printer with ``bambu_printer_manager``
(``bpm``), whose ``BambuPrinter`` object exposes a ``light_state`` property
(getter = real state, setter publishes the toggle over the open socket). We
reach it through OctoPrint 2.0 internals
(``printer.current_connection`` / ``printer._connection`` →
``ConnectedBambuPrinter._client`` → ``bpm.BambuPrinter``).

Every access is best-effort and defensive: any missing attribute or wrong type
means "not available" rather than a crash, so the caller can fall back to the
standalone MQTT client.
"""

from typing import Optional

# BambuConnector marks its connection with ``connector == "bambu"``.
_BAMBU_CONNECTOR = "bambu"


def _dbg(logger, msg, *args):
    """Best-effort DEBUG log; never raises."""
    if logger is not None:
        try:
            logger.debug("connector_led: " + msg, *args)
        except Exception:  # noqa: BLE001
            pass


def _resolve_connection(printer):
    """Return the active connection object, trying public then private APIs.

    OctoPrint 2.0 exposes the active connection via the public
    ``current_connection`` property; some builds/proxies only have the private
    ``_connection`` attribute, so fall back to that.
    """
    conn = getattr(printer, "current_connection", None)
    if callable(conn):
        try:
            conn = conn()
        except Exception:  # noqa: BLE001
            conn = None
    if conn is not None and not isinstance(conn, (bool, str, int)):
        return conn
    return getattr(printer, "_connection", None)


def _bambu_printer(printer, logger=None):
    """Return BambuConnector's ``bpm.BambuPrinter``, or ``None``.

    The chain is ``printer`` → active connection (``connector == "bambu"``) →
    its ``_client`` (a ``bpm.BambuPrinter``). The client is usable when it
    carries the ``light_state`` property. Logs (DEBUG) why it bailed so the live
    path is observable.
    """
    try:
        connection = _resolve_connection(printer)
        if connection is None:
            _dbg(logger, "no active connection on printer")
            return None
        connector = getattr(connection, "connector", None)
        if connector != _BAMBU_CONNECTOR:
            _dbg(
                logger, "active connection not bambu (connector=%r)", connector
            )
            return None
        client = getattr(connection, "_client", None)
        # bpm.BambuPrinter exposes light_state; require it so the API fits
        if client is None or not hasattr(client, "light_state"):
            _dbg(
                logger,
                "bambu connection has no light-capable client (%r)",
                client,
            )
            return None
        return client
    except Exception as exc:  # noqa: BLE001 - introspection must never raise
        _dbg(logger, "connection lookup raised: %r", exc)
        return None


def available(printer, logger=None) -> bool:
    """True when the light can be driven via BambuConnector's connection."""
    return _bambu_printer(printer, logger) is not None


def set_chamber_light(printer, on: bool, logger=None) -> bool:
    """Set the printer light over BambuConnector's connection.

    Returns ``True`` on success, ``False`` when the connector path is
    unavailable (so the caller can fall back to the standalone client). Never
    raises.
    """
    client = _bambu_printer(printer, logger)
    if client is None:
        return False
    try:
        client.light_state = bool(on)
        _dbg(logger, "set light_state=%s via connector", bool(on))
        return True
    except Exception as exc:  # noqa: BLE001 - any failure → fall back to MQTT
        _dbg(logger, "set light_state failed: %r", exc)
        return False


def current_state(printer, logger=None) -> Optional[bool]:
    """Return the real light state from BambuConnector, or ``None``.

    Reads ``bpm.BambuPrinter.light_state`` (a bool). Any missing attribute or
    error yields ``None`` (state unknown).
    """
    client = _bambu_printer(printer, logger)
    if client is None:
        return None
    try:
        return bool(client.light_state)
    except Exception:  # noqa: BLE001
        return None
