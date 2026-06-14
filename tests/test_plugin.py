"""Tests for octoprint_bambucam.BambucamPlugin."""

# pylint: disable=protected-access
# pylint: disable=import-outside-toplevel
# pylint: disable=too-few-public-methods

import logging
from unittest.mock import MagicMock, patch

import flask
import pytest

from octoprint_bambucam.daemon import WebcamdManager

# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestInit:
    """BambucamPlugin default state after __new__ + fixture injection."""

    def test_defaults(self, plugin):
        """Manager is None and webcam name is 'bambucam' at construction."""
        assert plugin._manager is None
        assert plugin._webcam_name == "bambucam"


# ---------------------------------------------------------------------------
# initialize
# ---------------------------------------------------------------------------


class TestInitialize:
    """initialize() wires up the WebcamdManager and HTTP logger."""

    def test_creates_manager(self, plugin):
        """A WebcamdManager instance is assigned to plugin._manager."""
        with patch(
            "octoprint_bambucam.WebcamdManager", autospec=True
        ) as mock_mgr_cls:
            mock_mgr = MagicMock()
            mock_mgr_cls.return_value = mock_mgr
            with patch.object(
                plugin, "_setup_http_logger", return_value=MagicMock()
            ):
                plugin.initialize()
        assert plugin._manager is mock_mgr

    def test_setup_http_logger_called(self, plugin):
        """_setup_http_logger is invoked exactly once during initialize()."""
        with patch("octoprint_bambucam.WebcamdManager"):
            with patch.object(
                plugin, "_setup_http_logger", return_value=MagicMock()
            ) as mock_setup:
                plugin.initialize()
        mock_setup.assert_called_once()


# ---------------------------------------------------------------------------
# _setup_http_logger
# ---------------------------------------------------------------------------


class TestSetupHttpLogger:
    """_setup_http_logger returns a named Logger with a file handler."""

    def test_returns_logger(self, plugin):
        """Return value is a Logger with the expected name."""
        with patch("logging.handlers.RotatingFileHandler"):
            log = plugin._setup_http_logger()
        assert isinstance(log, logging.Logger)
        assert log.name == "octoprint.plugins.bambucam.http"

    def test_no_duplicate_handlers(self, plugin):
        """Calling twice must not attach more than one handler."""
        with patch("logging.handlers.RotatingFileHandler"):
            plugin._setup_http_logger()
            log = plugin._setup_http_logger()
        assert len(log.handlers) <= 1


# ---------------------------------------------------------------------------
# on_after_startup
# ---------------------------------------------------------------------------


class TestOnAfterStartup:
    """on_after_startup conditionally starts the daemon based on settings."""

    def test_disabled_skips_start(self, plugin):
        """start() is not called when the plugin is disabled in settings."""
        plugin._settings.get_boolean = MagicMock(return_value=False)
        plugin._manager = MagicMock()
        plugin.on_after_startup()
        plugin._manager.start.assert_not_called()

    def test_no_hostname_skips_start(self, plugin):
        """start() is not called when hostname is empty."""
        plugin._settings.get_boolean = MagicMock(return_value=True)
        plugin._settings.get = MagicMock(return_value="")
        plugin._manager = MagicMock()
        plugin.on_after_startup()
        plugin._manager.start.assert_not_called()

    def test_no_access_code_skips_start(self, plugin):
        """start() is not called when access_code is empty."""
        plugin._settings.get_boolean = MagicMock(return_value=True)
        plugin._settings.get = MagicMock(
            side_effect=lambda keys: ("printer" if keys == ["hostname"] else "")
        )
        plugin._manager = MagicMock()
        plugin.on_after_startup()
        plugin._manager.start.assert_not_called()

    def test_configured_calls_start(self, plugin):
        """start() is called when enabled, hostname and access_code are set."""
        plugin._settings.get_boolean = MagicMock(return_value=True)
        plugin._settings.get = MagicMock(
            side_effect=lambda keys: (
                "printer" if keys == ["hostname"] else "secret"
            )
        )
        plugin._manager = MagicMock()
        plugin._manager.start.return_value = (True, None)
        with patch.object(plugin, "_daemon_config", return_value={}):
            plugin.on_after_startup()
        plugin._manager.start.assert_called_once()

    def test_start_failure_logs_error(self, plugin):
        """A failed start() is handled gracefully without raising."""
        plugin._settings.get_boolean = MagicMock(return_value=True)
        plugin._settings.get = MagicMock(return_value="value")
        plugin._manager = MagicMock()
        plugin._manager.start.return_value = (False, "port in use")
        with patch.object(plugin, "_daemon_config", return_value={}):
            plugin.on_after_startup()  # must not raise


# ---------------------------------------------------------------------------
# on_shutdown
# ---------------------------------------------------------------------------


class TestOnShutdown:
    """on_shutdown() stops the daemon if one is running."""

    def test_stop_called_when_manager_set(self, plugin):
        """stop() is called on the manager when it exists."""
        plugin._manager = MagicMock()
        plugin.on_shutdown()
        plugin._manager.stop.assert_called_once()

    def test_no_error_when_manager_none(self, plugin):
        """on_shutdown() is a no-op (no raise) when manager is None."""
        plugin._manager = None
        plugin.on_shutdown()  # must not raise


# ---------------------------------------------------------------------------
# get_settings_defaults
# ---------------------------------------------------------------------------


class TestGetSettingsDefaults:
    """get_settings_defaults() returns a complete set of typed defaults."""

    def test_all_keys_present(self, plugin):
        """All expected setting keys are returned."""
        defaults = plugin.get_settings_defaults()
        expected = {
            "enabled",
            "hostname",
            "access_code",
            "port",
            "bind_address",
            "stream_url_override",
            "width",
            "height",
            "rotate",
            "flashred",
            "showfps",
            "loghttp",
            "encodewait",
            "autorestart",
            "max_restarts",
            "restart_window",
        }
        assert set(defaults.keys()) == expected

    def test_default_values(self, plugin):
        """Key defaults are set to sensible out-of-box values."""
        d = plugin.get_settings_defaults()
        assert d["enabled"] is True
        assert d["port"] == 8181
        assert d["bind_address"] == "127.0.0.1"
        assert d["width"] == 1920
        assert d["height"] == 1080


# ---------------------------------------------------------------------------
# get_settings_restricted_paths
# ---------------------------------------------------------------------------


class TestGetSettingsRestrictedPaths:
    """get_settings_restricted_paths() guards sensitive settings."""

    def test_access_code_is_admin_only(self, plugin):
        """The access_code path requires admin permission."""
        paths = plugin.get_settings_restricted_paths()
        assert ["access_code"] in paths.get("admin", [])


# ---------------------------------------------------------------------------
# on_settings_save
# ---------------------------------------------------------------------------


class TestOnSettingsSave:
    """on_settings_save() restarts or stops the daemon when settings change."""

    def test_no_change_no_restart(self, plugin):
        """Saving unchanged values does not trigger a restart or stop."""
        plugin._settings.get = MagicMock(return_value="same")
        plugin._settings.get_boolean = MagicMock(return_value=True)
        plugin._manager = MagicMock()
        import octoprint.plugin as op_plugin

        with patch.object(
            op_plugin.SettingsPlugin, "on_settings_save", return_value=None
        ):
            plugin.on_settings_save({})
        plugin._manager.restart.assert_not_called()
        plugin._manager.stop.assert_not_called()

    def test_change_enabled_triggers_restart(self, plugin):
        """Changing a setting value when enabled triggers a daemon restart."""
        call_count = [0]

        def fake_get(_):
            call_count[0] += 1
            if call_count[0] <= 15:
                return "old"
            return "new"

        plugin._settings.get = fake_get
        plugin._settings.get_boolean = MagicMock(return_value=True)
        plugin._manager = MagicMock()
        plugin._manager.restart.return_value = (True, None)
        import octoprint.plugin as op_plugin

        ss_patch = patch.object(
            op_plugin.SettingsPlugin,
            "on_settings_save",
            return_value=None,
        )
        with ss_patch:
            with patch.object(plugin, "_daemon_config", return_value={}):
                plugin.on_settings_save({"hostname": "new"})
        plugin._manager.restart.assert_called_once()

    def test_change_disabled_calls_stop(self, plugin):
        """Changing a setting when disabled stops the daemon."""
        call_count = [0]

        def fake_get(_):
            call_count[0] += 1
            if call_count[0] <= 15:
                return "old"
            return "new"

        plugin._settings.get = fake_get
        plugin._settings.get_boolean = MagicMock(return_value=False)
        plugin._manager = MagicMock()
        import octoprint.plugin as op_plugin

        with patch.object(
            op_plugin.SettingsPlugin, "on_settings_save", return_value=None
        ):
            plugin.on_settings_save({"enabled": False})
        plugin._manager.stop.assert_called_once()


# ---------------------------------------------------------------------------
# is_template_autoescaped
# ---------------------------------------------------------------------------


class TestIsTemplateAutoescaped:
    """is_template_autoescaped() must return True for security."""

    def test_returns_true(self, plugin):
        """Autoescaping is always enabled in Jinja2 templates."""
        assert plugin.is_template_autoescaped() is True


# ---------------------------------------------------------------------------
# get_template_configs
# ---------------------------------------------------------------------------


class TestGetTemplateConfigs:
    """get_template_configs() advertises OctoPrint template extensions."""

    def test_two_templates(self, plugin):
        """Exactly two template entries are registered."""
        configs = plugin.get_template_configs()
        assert len(configs) == 2

    def test_settings_template(self, plugin):
        """Both 'settings' and 'webcam' template types are advertised."""
        types_ = [c["type"] for c in plugin.get_template_configs()]
        assert "settings" in types_
        assert "webcam" in types_


# ---------------------------------------------------------------------------
# get_assets
# ---------------------------------------------------------------------------


class TestGetAssets:
    """get_assets() advertises JavaScript and CSS bundles."""

    def test_js_and_css(self, plugin):
        """BambuCam.js and BambuCam.css are included in the asset manifest."""
        assets = plugin.get_assets()
        assert "js" in assets
        assert "css" in assets
        assert any("BambuCam.js" in f for f in assets["js"])
        assert any("BambuCam.css" in f for f in assets["css"])


# ---------------------------------------------------------------------------
# _loopback_url
# ---------------------------------------------------------------------------


class TestLoopbackUrl:
    """_loopback_url() builds http://127.0.0.1:<port>/?<action> URLs."""

    def test_format(self, plugin):
        """snapshot action produces the correct loopback URL."""
        plugin._settings.get_int = MagicMock(return_value=8181)
        url = plugin._loopback_url("snapshot")
        assert url == "http://127.0.0.1:8181/?snapshot"

    def test_stream_action(self, plugin):
        """stream action produces the correct loopback URL."""
        plugin._settings.get_int = MagicMock(return_value=8181)
        url = plugin._loopback_url("stream")
        assert url == "http://127.0.0.1:8181/?stream"


# ---------------------------------------------------------------------------
# _stream_url
# ---------------------------------------------------------------------------


class TestStreamUrl:
    """_stream_url() returns the override URL or falls back to loopback."""

    def test_override_wins(self, plugin):
        """A non-empty stream_url_override is returned as-is."""
        override = "http://override.example/stream"
        plugin._settings.get = MagicMock(return_value=override)
        url = plugin._stream_url()
        assert url == override

    def test_default_loopback(self, plugin):
        """Empty override falls back to the loopback stream URL."""
        plugin._settings.get = MagicMock(return_value="")
        plugin._settings.get_int = MagicMock(return_value=8181)
        url = plugin._stream_url()
        assert "127.0.0.1" in url
        assert "stream" in url


# ---------------------------------------------------------------------------
# _daemon_config
# ---------------------------------------------------------------------------


class TestDaemonConfig:
    """_daemon_config() assembles the full daemon configuration dict."""

    def test_all_keys_present(self, plugin):
        """All required config keys are present in the returned dict."""
        plugin._settings.get = MagicMock(return_value="val")
        plugin._settings.get_int = MagicMock(return_value=1)
        plugin._settings.get_boolean = MagicMock(return_value=False)
        plugin._settings.get_float = MagicMock(return_value=0.5)
        cfg = plugin._daemon_config()
        expected_keys = {
            "hostname",
            "access_code",
            "port",
            "bind_address",
            "width",
            "height",
            "rotate",
            "flashred",
            "showfps",
            "loghttp",
            "encodewait",
            "autorestart",
            "max_restarts",
            "restart_window",
        }
        assert set(cfg.keys()) == expected_keys


# ---------------------------------------------------------------------------
# get_webcam_configurations
# ---------------------------------------------------------------------------


class TestGetWebcamConfigurations:
    """get_webcam_configurations() advertises the BambuCam webcam."""

    def test_returns_list_with_one_webcam(self, plugin):
        """Exactly one webcam is returned."""
        plugin._settings.get = MagicMock(return_value="")
        plugin._settings.get_int = MagicMock(return_value=8181)
        cams = plugin.get_webcam_configurations()
        assert len(cams) == 1

    def test_webcam_name(self, plugin):
        """The webcam is identified by the 'bambucam' name."""
        plugin._settings.get = MagicMock(return_value="")
        plugin._settings.get_int = MagicMock(return_value=8181)
        cam = plugin.get_webcam_configurations()[0]
        assert cam.name == "bambucam"


# ---------------------------------------------------------------------------
# take_webcam_snapshot
# ---------------------------------------------------------------------------


class TestTakeWebcamSnapshot:
    """take_webcam_snapshot() fetches a JPEG from the local daemon."""

    def _snap_exc(self):
        """Return the WebcamNotAbleToTakeSnapshotException class."""
        import sys

        return sys.modules[
            "octoprint.webcams"
        ].WebcamNotAbleToTakeSnapshotException

    def test_raises_when_not_running(self, plugin):
        """Raises WebcamNotAbleToTakeSnapshotException when manager is None."""
        plugin._manager = None
        with pytest.raises(self._snap_exc()):
            list(plugin.take_webcam_snapshot("bambucam"))

    def test_raises_when_manager_not_running(self, plugin):
        """Raises when the daemon process is not running."""
        exc_cls = self._snap_exc()
        plugin._manager = MagicMock()
        plugin._manager.is_running.return_value = False
        with pytest.raises(exc_cls):
            list(plugin.take_webcam_snapshot("bambucam"))

    def test_yields_bytes(self, plugin):
        """Returns JPEG bytes fetched from the local webcam endpoint."""
        plugin._manager = MagicMock()
        plugin._manager.is_running.return_value = True
        plugin._settings.get_int = MagicMock(return_value=8181)

        mock_resp = MagicMock()
        mock_resp.read.return_value = b"\xff\xd8\xff"
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = list(plugin.take_webcam_snapshot("bambucam"))

        assert result == [b"\xff\xd8\xff"]


# ---------------------------------------------------------------------------
# get_api_commands / is_api_protected
# ---------------------------------------------------------------------------


class TestApiMeta:
    """API metadata: command registry and protection flag."""

    def test_get_api_commands(self, plugin):
        """restart, test_connection and fetch_info commands are registered."""
        cmds = plugin.get_api_commands()
        assert "restart" in cmds
        assert "test_connection" in cmds
        assert "fetch_info" in cmds

    def test_is_api_protected(self, plugin):
        """API endpoints are protected (require authentication)."""
        assert plugin.is_api_protected() is True


# ---------------------------------------------------------------------------
# on_api_get
# ---------------------------------------------------------------------------


class TestOnApiGet:
    """on_api_get() returns daemon status enriched with the stream URL."""

    def _ctx(self):
        """Return a Flask test request context."""
        app = flask.Flask(__name__)
        return app.test_request_context()

    def test_returns_status(self, plugin):
        """JSON response includes running flag and stream_url."""
        plugin._manager = MagicMock()
        plugin._manager.status.return_value = {"running": True}
        plugin._settings.get = MagicMock(return_value="")
        plugin._settings.get_int = MagicMock(return_value=8181)

        perms_mod = __import__(
            "octoprint.access.permissions", fromlist=["Permissions"]
        )
        perms_mod.Permissions.SETTINGS.can = MagicMock(return_value=True)

        with self._ctx():
            response = plugin.on_api_get(MagicMock())
        data = response.get_json()
        assert data["running"] is True
        assert "stream_url" in data

    def test_403_without_permission(self, plugin):
        """Raises an exception when the caller lacks SETTINGS permission."""
        perms_mod = __import__(
            "octoprint.access.permissions", fromlist=["Permissions"]
        )
        perms_mod.Permissions.SETTINGS.can = MagicMock(return_value=False)
        plugin._manager = MagicMock()

        with self._ctx():
            with pytest.raises(Exception):
                plugin.on_api_get(MagicMock())


# ---------------------------------------------------------------------------
# on_api_command
# ---------------------------------------------------------------------------


class TestOnApiCommand:
    """on_api_command() dispatches restart, test_connection, fetch_info."""

    def _ctx(self):
        """Return a Flask test request context."""
        app = flask.Flask(__name__)
        return app.test_request_context()

    def test_restart_command(self, plugin):
        """restart command returns ok=True on success."""
        plugin._manager = MagicMock()
        plugin._manager.restart.return_value = (True, None)
        perms_mod = __import__(
            "octoprint.access.permissions", fromlist=["Permissions"]
        )
        perms_mod.Permissions.ADMIN.can = MagicMock(return_value=True)

        with self._ctx():
            with patch.object(plugin, "_daemon_config", return_value={}):
                resp = plugin.on_api_command("restart", {})
        assert resp.get_json()["ok"] is True

    def test_restart_requires_admin(self, plugin):
        """restart command raises when the caller lacks ADMIN permission."""
        plugin._manager = MagicMock()
        perms_mod = __import__(
            "octoprint.access.permissions", fromlist=["Permissions"]
        )
        perms_mod.Permissions.ADMIN.can = MagicMock(return_value=False)

        with self._ctx():
            with pytest.raises(Exception):
                plugin.on_api_command("restart", {})

    def test_fetch_info_command(self, plugin):
        """fetch_info returns ok=True and the info dict on success."""
        plugin._manager = MagicMock()
        plugin._manager.fetch_info.return_value = {"version": "1.0"}
        perms_mod = __import__(
            "octoprint.access.permissions", fromlist=["Permissions"]
        )
        perms_mod.Permissions.SETTINGS.can = MagicMock(return_value=True)

        with self._ctx():
            resp = plugin.on_api_command("fetch_info", {})
        data = resp.get_json()
        assert data["ok"] is True
        assert data["info"] == {"version": "1.0"}

    def test_fetch_info_unreachable(self, plugin):
        """fetch_info returns ok=False with reason 'unreachable' on None."""
        plugin._manager = MagicMock()
        plugin._manager.fetch_info.return_value = None
        perms_mod = __import__(
            "octoprint.access.permissions", fromlist=["Permissions"]
        )
        perms_mod.Permissions.SETTINGS.can = MagicMock(return_value=True)

        with self._ctx():
            resp = plugin.on_api_command("fetch_info", {})
        data = resp.get_json()
        assert data["ok"] is False
        assert data["reason"] == "unreachable"

    def test_fetch_info_requires_settings_perm(self, plugin):
        """fetch_info raises when caller lacks SETTINGS permission."""
        plugin._manager = MagicMock()
        perms_mod = __import__(
            "octoprint.access.permissions", fromlist=["Permissions"]
        )
        perms_mod.Permissions.SETTINGS.can = MagicMock(return_value=False)

        with self._ctx():
            with pytest.raises(Exception):
                plugin.on_api_command("fetch_info", {})

    def test_test_connection_command_success(self, plugin):
        """test_connection returns ok=True and reason 'ok' on success."""
        plugin._manager = MagicMock()
        perms_mod = __import__(
            "octoprint.access.permissions", fromlist=["Permissions"]
        )
        perms_mod.Permissions.ADMIN.can = MagicMock(return_value=True)

        tc_patch = patch.object(
            WebcamdManager,
            "test_connection",
            return_value=(True, "ok"),
        )
        with self._ctx():
            with tc_patch:
                resp = plugin.on_api_command(
                    "test_connection",
                    {
                        "hostname": "printer.local",
                        "access_code": "secret",
                    },
                )
        data = resp.get_json()
        assert data["ok"] is True
        assert data["reason"] == "ok"

    def test_test_connection_timeout(self, plugin):
        """test_connection returns ok=False, reason 'timeout' on timeout."""
        plugin._manager = MagicMock()
        perms_mod = __import__(
            "octoprint.access.permissions", fromlist=["Permissions"]
        )
        perms_mod.Permissions.ADMIN.can = MagicMock(return_value=True)

        mock_event = MagicMock()
        mock_event.wait.return_value = False  # simulate timeout

        mock_thread = MagicMock()
        mock_thread.start.return_value = None

        ev_patch = patch(
            "octoprint_bambucam.threading.Event",
            return_value=mock_event,
        )
        th_patch = patch(
            "octoprint_bambucam.threading.Thread",
            return_value=mock_thread,
        )
        with self._ctx():
            with ev_patch:
                with th_patch:
                    resp = plugin.on_api_command(
                        "test_connection",
                        {
                            "hostname": "printer.local",
                            "access_code": "secret",
                        },
                    )
        data = resp.get_json()
        assert data["ok"] is False
        assert data["reason"] == "timeout"


# ---------------------------------------------------------------------------
# get_update_information
# ---------------------------------------------------------------------------


class TestGetUpdateInformation:
    """get_update_information() registers the plugin with update manager."""

    def test_returns_bambucam_key(self, plugin):
        """The returned dict contains the 'bambucam' key."""
        info = plugin.get_update_information()
        assert "bambucam" in info

    def test_github_url_present(self, plugin):
        """The pip URL points to the correct GitHub repository."""
        info = plugin.get_update_information()
        pip = info["bambucam"]["pip"]
        # Assert on the full host prefix rather than a bare "github.com"
        # substring (the latter trips CodeQL's URL-sanitization heuristic and
        # would also pass for e.g. "github.com.evil.test").
        assert "https://github.com/" in pip
        assert "OctoPrint-BambuCam" in pip

    def test_version_matches(self, plugin):
        """The current version in update info matches the plugin version."""
        info = plugin.get_update_information()
        assert info["bambucam"]["current"] == plugin._plugin_version


# ---------------------------------------------------------------------------
# _on_daemon_state
# ---------------------------------------------------------------------------


class TestOnDaemonState:
    """_on_daemon_state() broadcasts daemon state changes to the frontend."""

    def test_sends_plugin_message(self, plugin):
        """A plugin message with type, state and detail is sent."""
        plugin._on_daemon_state("crashed", {"returncode": 1})
        plugin._plugin_manager.send_plugin_message.assert_called_once_with(
            "bambucam",
            {
                "type": "daemon_state",
                "state": "crashed",
                "detail": {"returncode": 1},
            },
        )
