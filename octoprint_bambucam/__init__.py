"""BambuCam OctoPrint plugin.

Provides a webcam stream and snapshot endpoint for Bambu Lab printers by
managing a ``webcamd`` daemon and exposing it through OctoPrint's webcam,
settings, template and simple-API plugin mixins.
"""

import logging
import logging.handlers
import threading
import urllib.request
from typing import TYPE_CHECKING, Optional

import flask
import octoprint.plugin
from octoprint.access.permissions import Permissions
from octoprint.schema.webcam import RatioEnum, Webcam, WebcamCompatibility
from octoprint.webcams import WebcamNotAbleToTakeSnapshotException

from .daemon import WebcamdManager

if TYPE_CHECKING:
    from octoprint.plugin import PluginSettings
    from octoprint.plugin.core import PluginManager

# settings keys that require a daemon restart when changed
DAEMON_SETTINGS = (
    "enabled",
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
)


class BambucamPlugin(
    octoprint.plugin.StartupPlugin,
    octoprint.plugin.ShutdownPlugin,
    octoprint.plugin.SettingsPlugin,
    octoprint.plugin.TemplatePlugin,
    octoprint.plugin.AssetPlugin,
    octoprint.plugin.SimpleApiPlugin,
    octoprint.plugin.WebcamProviderPlugin,
):
    """Manage the BambuCam ``webcamd`` daemon and expose it to OctoPrint.

    Note: several OctoPrint mixin hooks (the SimpleApiPlugin methods,
    ``is_template_autoescaped``) are annotated with overly narrow return types
    in OctoPrint's base classes (``-> None`` / ``Literal[False]``) even though
    the real plugin API expects Flask responses / dicts / bools. We return the
    correct values here; ``reportIncompatibleMethodOverride`` is disabled in
    ``pyrightconfig.json`` so those accurate overrides do not get flagged.
    """

    # These attributes are injected by OctoPrint's plugin core after
    # construction; declaring them here gives type checkers the real types
    # instead of the ``None`` placeholders set in the mixin constructors.
    _settings: "PluginSettings"
    _plugin_manager: "PluginManager"
    _logger: logging.Logger
    _identifier: str
    _plugin_version: str

    def __init__(self):
        super().__init__()
        self._manager: Optional[WebcamdManager] = None
        self._webcam_name = "bambucam"

    def initialize(self):
        self._manager = WebcamdManager(
            self._logger,
            on_state_change=self._on_daemon_state,
            http_logger=self._setup_http_logger(),
        )

    def _setup_http_logger(self):
        """A dedicated logger that writes the stream server's HTTP request log
        (emitted when ``--loghttp`` is on) to its own rotating file in the
        OctoPrint logs folder, separate from the main plugin log."""
        logger = logging.getLogger("octoprint.plugins.bambucam.http")
        logger.setLevel(logging.INFO)
        logger.propagate = False
        if not logger.handlers:
            handler = logging.handlers.RotatingFileHandler(
                self._settings.get_plugin_logfile_path(postfix="http"),
                maxBytes=2 * 1024 * 1024,
                backupCount=3,
            )
            handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
            logger.addHandler(handler)
        return logger

    # ~~ StartupPlugin mixin

    def on_after_startup(self):
        if not self._settings.get_boolean(["enabled"]):
            self._logger.info("BambuCam is disabled, not starting webcamd")
            return
        if not self._settings.get(["hostname"]) or not self._settings.get(
            ["access_code"]
        ):
            self._logger.info(
                "BambuCam is not configured yet, not starting webcamd"
            )
            return
        assert self._manager is not None  # set in initialize()
        ok, error = self._manager.start(self._daemon_config())
        if not ok:
            self._logger.error("could not start webcamd: %s", error)

    # ~~ ShutdownPlugin mixin

    def on_shutdown(self):
        if self._manager is not None:
            self._manager.stop()

    # ~~ SettingsPlugin mixin

    def get_settings_defaults(self):
        return {
            "enabled": True,
            "hostname": "",
            "access_code": "",
            "port": 8181,
            "bind_address": "127.0.0.1",
            "stream_url_override": "",
            "width": 1920,
            "height": 1080,
            "rotate": -1,
            "flashred": False,
            "showfps": False,
            "loghttp": False,
            "encodewait": 0.5,
            "autorestart": True,
            "max_restarts": 5,
            "restart_window": 300,
        }

    def get_settings_restricted_paths(self):
        return {"admin": [["access_code"]]}

    def on_settings_save(self, data):
        old = {key: self._settings.get([key]) for key in DAEMON_SETTINGS}
        result = octoprint.plugin.SettingsPlugin.on_settings_save(self, data)
        new = {key: self._settings.get([key]) for key in DAEMON_SETTINGS}

        if old == new:
            return result

        assert self._manager is not None  # set in initialize()
        if self._settings.get_boolean(["enabled"]):
            self._logger.info(
                "daemon-relevant settings changed, restarting webcamd"
            )
            ok, error = self._manager.restart(self._daemon_config())
            if not ok:
                self._logger.error("could not restart webcamd: %s", error)
        else:
            self._manager.stop()
        return result

    # ~~ TemplatePlugin mixin

    def is_template_autoescaped(self) -> bool:
        return True

    def get_template_configs(self):
        return [
            {
                "type": "settings",
                "template": "bambucam_settings.jinja2",
                "custom_bindings": True,
            },
            {
                "type": "webcam",
                "name": "BambuCam",
                "template": "bambucam_webcam.jinja2",
                "custom_bindings": True,
            },
        ]

    # ~~ AssetPlugin mixin

    def get_assets(self):
        return {
            "js": ["js/BambuCam.js"],
            "css": ["css/BambuCam.css"],
        }

    # ~~ WebcamProviderPlugin mixin

    def get_webcam_configurations(self):
        snapshot_url = self._loopback_url("snapshot")
        return [
            Webcam(
                name=self._webcam_name,
                displayName="BambuCam",
                canSnapshot=True,
                snapshotDisplay=snapshot_url,
                compat=WebcamCompatibility(
                    stream=self._stream_url(),
                    streamRatio=RatioEnum.sixteen_nine,
                    snapshot=snapshot_url,
                ),
                extras={
                    "stream": self._stream_url(),
                    "port": self._settings.get_int(["port"]),
                },
            )
        ]

    def take_webcam_snapshot(self, webcamName):
        if self._manager is None or not self._manager.is_running():
            raise WebcamNotAbleToTakeSnapshotException(self._webcam_name)
        with urllib.request.urlopen(  # nosec B310 - fixed http://127.0.0.1 URL
            self._loopback_url("snapshot"), timeout=10
        ) as response:
            yield response.read()

    # ~~ SimpleApiPlugin mixin

    def get_api_commands(self) -> dict:
        return {
            "restart": [],
            "test_connection": ["hostname", "access_code"],
            "fetch_info": [],
        }

    def is_api_protected(self) -> bool:
        return True

    def on_api_get(self, request) -> flask.Response:
        if not Permissions.SETTINGS.can():
            flask.abort(403)
        assert self._manager is not None  # set in initialize()
        status = self._manager.status()
        status["stream_url"] = self._stream_url()
        return flask.jsonify(status)

    def on_api_command(self, command, data) -> Optional[flask.Response]:
        # fetch_info only reads (and the password is already redacted by the
        # vendored webcam.py), so SETTINGS is enough; the rest needs ADMIN.
        assert self._manager is not None  # set in initialize()
        if command == "fetch_info":
            if not Permissions.SETTINGS.can():
                flask.abort(403)
            info = self._manager.fetch_info()
            if info is None:
                return flask.jsonify(ok=False, reason="unreachable")
            return flask.jsonify(ok=True, info=info)

        if not Permissions.ADMIN.can():
            flask.abort(403)

        if command == "restart":
            ok, error = self._manager.restart(self._daemon_config())
            return flask.jsonify(ok=ok, error=error)

        if command == "test_connection":
            result = {}
            done = threading.Event()

            def probe():
                ok, reason = WebcamdManager.test_connection(
                    data["hostname"], data["access_code"]
                )
                result["ok"] = ok
                result["reason"] = reason
                done.set()

            threading.Thread(target=probe, daemon=True).start()
            # the probe has its own 10 s socket timeout; cap the request
            # slightly above
            if not done.wait(timeout=12):
                return flask.jsonify(ok=False, reason="timeout")
            return flask.jsonify(**result)

    # ~~ Softwareupdate hook

    def get_update_information(self):
        """Software-update hook config consumed by OctoPrint's updater."""
        return {
            "bambucam": {
                "displayName": "BambuCam",
                "displayVersion": self._plugin_version,
                "type": "github_release",
                "user": "Ajimaru",
                "repo": "OctoPrint-BambuCam",
                "current": self._plugin_version,
                "pip": "https://github.com/Ajimaru/OctoPrint-BambuCam"
                "/archive/{target_version}.zip",
            }
        }

    # ~~ helpers

    def _daemon_config(self):
        return {
            "hostname": self._settings.get(["hostname"]),
            "access_code": self._settings.get(["access_code"]),
            "port": self._settings.get_int(["port"]),
            "bind_address": self._settings.get(["bind_address"]),
            "width": self._settings.get_int(["width"]),
            "height": self._settings.get_int(["height"]),
            "rotate": self._settings.get_int(["rotate"]),
            "flashred": self._settings.get_boolean(["flashred"]),
            "showfps": self._settings.get_boolean(["showfps"]),
            "loghttp": self._settings.get_boolean(["loghttp"]),
            "encodewait": self._settings.get_float(["encodewait"]),
            "autorestart": self._settings.get_boolean(["autorestart"]),
            "max_restarts": self._settings.get_int(["max_restarts"]),
            "restart_window": self._settings.get_int(["restart_window"]),
        }

    def _loopback_url(self, action):
        """Build the local ``webcamd`` URL for the given action (e.g. the
        ``stream`` or ``snapshot`` endpoint)."""
        port = self._settings.get_int(["port"])
        return f"http://127.0.0.1:{port}/?{action}"

    def _stream_url(self):
        """Stream URL for browsers. An override wins; otherwise the loopback
        URL is returned, which only works when the browser runs on the
        OctoPrint host — the frontend viewmodel rewrites it to the current
        browser host when the bind address is 0.0.0.0 (see BambuCam.js)."""
        override = self._settings.get(["stream_url_override"])
        if override:
            return override
        return self._loopback_url("stream")

    def _on_daemon_state(self, state, detail):
        self._plugin_manager.send_plugin_message(
            self._identifier,
            {"type": "daemon_state", "state": state, "detail": detail},
        )


__plugin_name__ = "BambuCam"
__plugin_pythoncompat__ = ">=3.7,<4"
__plugin_implementation__ = BambucamPlugin()
__plugin_hooks__ = {
    "octoprint.plugin.softwareupdate.check_config": (
        __plugin_implementation__.get_update_information
    )
}
