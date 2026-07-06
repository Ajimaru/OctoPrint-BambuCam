"""Tests for best-effort OctoPrint-BambuConnector discovery."""

from octoprint_bambucam import bambu_connector


class _FakePluginInfo:
    def __init__(self, enabled=True):
        self.enabled = enabled


class _FakePluginManager:
    """Minimal stand-in exposing the ``.plugins`` mapping ``detect`` reads."""

    def __init__(self, plugins=None):
        self.plugins = plugins or {}


class _FakeSettings:
    """Path-aware ``global_get`` for the two locations ``detect`` reads.

    ``plugin_data`` is returned for ``["plugins", <id>]`` and ``profile`` for
    ``["printerConnection", "preferred"]`` — mirroring the real config layout.
    """

    def __init__(self, plugin_data=None, profile=None, raises=False):
        self._data = plugin_data
        self._profile = profile
        self._raises = raises

    def global_get(self, path, merged=False):
        """Return the profile for the connection path, else the plugin data."""
        del merged
        if self._raises:
            raise RuntimeError("boom")
        if path and path[0] == "printerConnection":
            return self._profile
        return self._data


def _pm_with_connector(enabled=True):
    """Build a plugin manager that reports the connector plugin present."""
    return _FakePluginManager(
        {bambu_connector.CONNECTOR_PLUGIN_ID: _FakePluginInfo(enabled)}
    )


def test_not_installed_is_unavailable():
    """Connector absent from the plugin manager is reported unavailable."""
    info = bambu_connector.detect(_FakePluginManager({}), _FakeSettings())
    assert info.installed is False
    assert info.available is False
    assert info.as_dict()["has_access_code"] is False


def test_installed_but_no_data_is_unavailable():
    """Installed connector with empty settings is unavailable."""
    info = bambu_connector.detect(
        _pm_with_connector(), _FakeSettings(plugin_data={})
    )
    assert info.installed is True
    assert info.available is False


def test_top_level_host_and_code_available():
    """Top-level host and access code mark the connector available."""
    settings = _FakeSettings(
        plugin_data={"host": "192.168.1.5", "access_code": "abc123"}
    )
    info = bambu_connector.detect(_pm_with_connector(), settings)
    assert info.available is True
    assert info.hostname == "192.168.1.5"
    assert info.access_code == "abc123"
    # the plaintext code is never serialized to the browser
    d = info.as_dict()
    assert d["has_access_code"] is True
    assert "abc123" not in str(d)


def test_nested_connection_block_is_flattened():
    """A nested ``connection`` block is flattened into the info fields."""
    settings = _FakeSettings(
        plugin_data={
            "connection": {
                "hostname": "printer.local",
                "code": "xyz",
                "serial": "01S00A",
            }
        }
    )
    info = bambu_connector.detect(_pm_with_connector(), settings)
    assert info.available is True
    assert info.hostname == "printer.local"
    assert info.access_code == "xyz"
    assert info.serial == "01S00A"


def test_host_without_code_is_unavailable():
    """A host without an access code cannot auto-fill, so unavailable."""
    settings = _FakeSettings(plugin_data={"host": "10.0.0.2"})
    info = bambu_connector.detect(_pm_with_connector(), settings)
    assert info.installed is True
    assert info.available is False  # no access code → cannot auto-fill


def test_disabled_plugin_is_unavailable():
    """A disabled connector plugin is treated as not installed."""
    settings = _FakeSettings(
        plugin_data={"host": "10.0.0.2", "access_code": "k"}
    )
    info = bambu_connector.detect(_pm_with_connector(enabled=False), settings)
    assert info.installed is False
    assert info.available is False


def test_settings_read_error_never_raises():
    """A failing settings read degrades to unavailable without raising."""
    info = bambu_connector.detect(
        _pm_with_connector(), _FakeSettings(raises=True)
    )
    # installed but unreadable settings → degrade to unavailable, no exception
    assert info.installed is True
    assert info.available is False


def test_plugin_manager_without_plugins_attr_is_safe():
    """A plugin manager lacking ``.plugins`` is handled without error."""
    info = bambu_connector.detect(object(), _FakeSettings())
    assert info.installed is False
    assert info.available is False


# ── connection profile (the real-world location) ──────────────────────────


def test_connection_profile_bambu_is_available():
    """host/access_code/serial from the Bambu connection profile are used."""
    profile = {
        "connector": "bambu",
        "parameters": {
            "host": "192.168.1.123",
            "access_code": "12345678",
            "serial": "0300DA600000000",
        },
    }
    info = bambu_connector.detect(
        _pm_with_connector(),
        _FakeSettings(
            plugin_data={"printer_timezone": "Europe/Berlin"}, profile=profile
        ),
    )
    assert info.available is True
    assert info.hostname == "192.168.1.123"
    assert info.access_code == "12345678"
    assert info.serial == "0300DA600000000"


def test_connection_profile_non_bambu_is_ignored():
    """A non-Bambu connection profile must not be used."""
    profile = {
        "connector": "serial",
        "parameters": {"host": "/dev/ttyUSB0", "access_code": "x"},
    }
    info = bambu_connector.detect(
        _pm_with_connector(), _FakeSettings(profile=profile)
    )
    assert info.installed is True
    assert info.available is False


def test_real_world_layout_timezone_only_settings():
    """Reproduces the live A1 mini: plugin settings only hold the timezone,
    the connection data lives on the Bambu profile → still available."""
    info = bambu_connector.detect(
        _pm_with_connector(),
        _FakeSettings(
            plugin_data={"printer_timezone": "Europe/Berlin"},
            profile={
                "connector": "bambu",
                "parameters": {
                    "host": "192.168.1.123",
                    "access_code": "12345678",
                    "serial": "0300DA600000000",
                },
            },
        ),
    )
    assert info.available is True
