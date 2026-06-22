"""Best-effort discovery of connection data from OctoPrint-BambuConnector.

If that plugin (id ``bambu_connector``) is installed, it already knows the
printer's host/serial/access_code, so we can offer an "auto" config mode that
reuses them. It keeps those on the OctoPrint 2.0 connection profile
(``printerConnection.preferred.parameters``), not in plain plugin settings, so
detection reads the profile (when ``connector == "bambu"``) and falls back to
the plugin settings. Best-effort throughout: never raises, never hits the
network, reports ``available=False`` (→ manual entry) when nothing is trusted.
"""

from typing import Optional

CONNECTOR_PLUGIN_ID = "bambu_connector"

_CONNECTION_PROFILE_PATH = ["printerConnection", "preferred"]
_BAMBU_CONNECTOR_VALUE = "bambu"
_HOST_KEYS = ("host", "hostname", "ip", "address")
_CODE_KEYS = ("access_code", "accessCode", "code")
_SERIAL_KEYS = ("serial", "serial_number", "serialNumber")


class ConnectorInfo:
    """What we could discover about the printer from BambuConnector.

    ``available`` is True only when the plugin is installed *and* we found at
    least a host + access code we can reuse. ``hostname``/``access_code``/
    ``serial`` may be ``None`` individually.
    """

    def __init__(
        self,
        *,
        installed: bool = False,
        hostname: Optional[str] = None,
        access_code: Optional[str] = None,
        serial: Optional[str] = None,
    ):
        self.installed = installed
        self.hostname = hostname or None
        self.access_code = access_code or None
        self.serial = serial or None

    @property
    def available(self) -> bool:
        """True when there is enough to auto-fill the connection fields."""
        return bool(self.installed and self.hostname and self.access_code)

    def as_dict(self) -> dict:
        """Serialize for the frontend. The access code is never sent back to
        the browser as plaintext — only whether one is present."""
        return {
            "installed": self.installed,
            "available": self.available,
            "hostname": self.hostname or "",
            "serial": self.serial or "",
            "has_access_code": bool(self.access_code),
        }


def _first(d: dict, keys) -> Optional[str]:
    """Return the first non-empty string value among ``keys`` in ``d``."""
    if not isinstance(d, dict):
        return None
    for key in keys:
        val = d.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def _is_installed(plugin_manager) -> bool:
    try:
        plugins = getattr(plugin_manager, "plugins", {}) or {}
        info = plugins.get(CONNECTOR_PLUGIN_ID)
        return bool(info and getattr(info, "enabled", True))
    except Exception:  # noqa: BLE001 - discovery must never raise
        return False


def _params_from_settings(settings) -> dict:
    """Read whatever BambuConnector left under its own plugin settings.

    On OctoPrint 1.x most of the connection data lives on the 2.0 connection
    profile (not reachable here), but some builds also mirror values into the
    plugin settings — so we try this cheap, safe read first.
    """
    try:
        raw = settings.global_get(["plugins", CONNECTOR_PLUGIN_ID], merged=True)
    except Exception:  # noqa: BLE001
        return {}
    return raw if isinstance(raw, dict) else {}


def _params_from_connection_profile(settings) -> dict:
    """Read the OctoPrint 2.0 connection profile, if it is a Bambu one.

    This is where BambuConnector actually keeps the live host/serial/
    access_code (``printerConnection.preferred.parameters``). Returns the
    parameters dict only when ``connector == "bambu"``, else an empty dict.
    """
    try:
        preferred = settings.global_get(_CONNECTION_PROFILE_PATH, merged=True)
    except Exception:  # noqa: BLE001
        return {}
    if not isinstance(preferred, dict):
        return {}
    if preferred.get("connector") != _BAMBU_CONNECTOR_VALUE:
        return {}
    params = preferred.get("parameters")
    return params if isinstance(params, dict) else {}


def detect(plugin_manager, settings) -> ConnectorInfo:
    """Best-effort probe for BambuConnector's printer connection data.

    Never raises and never touches the network. Returns a
    :class:`ConnectorInfo`; when nothing trustworthy is found, ``available`` is
    False and the caller should leave the user in manual-entry mode.
    """
    installed = _is_installed(plugin_manager)
    if not installed:
        return ConnectorInfo(installed=False)

    profile = _params_from_connection_profile(settings)
    data = _params_from_settings(settings)
    candidates = [profile, data]

    for sub in ("connection", "printer", "parameters", "last_connection"):
        nested = data.get(sub) if isinstance(data, dict) else None
        if isinstance(nested, dict):
            candidates.append(nested)

    hostname = access_code = serial = None
    for cand in candidates:
        hostname = hostname or _first(cand, _HOST_KEYS)
        access_code = access_code or _first(cand, _CODE_KEYS)
        serial = serial or _first(cand, _SERIAL_KEYS)

    return ConnectorInfo(
        installed=True,
        hostname=hostname,
        access_code=access_code,
        serial=serial,
    )
