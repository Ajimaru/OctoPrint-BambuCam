"""BambuCam OctoPrint plugin.

Provides a webcam stream and snapshot endpoint for Bambu Lab printers by
managing a ``webcamd`` daemon and exposing it through OctoPrint's webcam,
settings, template and simple-API plugin mixins.
"""

import datetime
import logging
import logging.handlers
import os
import threading
import urllib.request
from typing import TYPE_CHECKING, Optional

import flask
import octoprint.plugin
from octoprint.access.permissions import Permissions
from octoprint.schema.webcam import RatioEnum, Webcam, WebcamCompatibility
from octoprint.webcams import WebcamNotAbleToTakeSnapshotException

from . import bambu_connector, connector_led
from ._version import VERSION as _PLUGIN_VERSION
from .autosync import AutoSyncMixin
from .daemon import WebcamdManager
from .ftp import BambuTimelapseFtp, FtpError
from .mqtt import BambuMqttClient, BambuMqttMonitor, MqttError
from .paths import sanitize_filename
from .timelapse_ops import TimelapseOpsMixin

if TYPE_CHECKING:
    from octoprint.plugin import PluginSettings
    from octoprint.plugin.core import PluginManager
    from octoprint.printer import PrinterInterface


# settings keys that require a daemon restart when changed
DAEMON_SETTINGS = (
    "enabled",
    "hostname",
    "access_code",
    "port",
    "bind_address",
    "override_resolution",
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
    TimelapseOpsMixin,
    AutoSyncMixin,
    octoprint.plugin.StartupPlugin,
    octoprint.plugin.ShutdownPlugin,
    octoprint.plugin.SettingsPlugin,
    octoprint.plugin.TemplatePlugin,
    octoprint.plugin.AssetPlugin,
    octoprint.plugin.SimpleApiPlugin,
    octoprint.plugin.EventHandlerPlugin,
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
    _printer: "PrinterInterface"

    def __init__(self):
        super().__init__()
        self._manager: Optional[WebcamdManager] = None
        self._webcam_name = "bambucam"
        self._ftp_lock = threading.Lock()
        self._ftp_busy = False
        self._thumb_lock = threading.Lock()
        self._led_busy = False
        self._led_lock = threading.Lock()
        self._led_monitor: Optional[BambuMqttMonitor] = None
        self._led_state: Optional[bool] = None
        self._init_autosync()

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
        if self._manager is None:
            self._logger.error("webcamd manager not initialized")
            return
        ok, error = self._manager.start(self._daemon_config())
        if not ok:
            self._logger.error("could not start webcamd: %s", error)

    def on_shutdown(self):
        self._stop_led_monitor()
        if self._manager is not None:
            self._manager.stop()

    def get_settings_defaults(self):
        return {
            "enabled": True,
            "config_source": "manual",
            "hostname": "",
            "access_code": "",
            "port": 8181,
            "bind_address": "127.0.0.1",
            "stream_url_override": "",
            "override_resolution": False,
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
            "download_suffix": "",
            "transcode_to_mp4": True,
            "auto_sync": False,
            # The A1 mini usually finishes rendering its timelapse *during*
            # the print, but not always: a measurement (plan §10.8) saw the
            # .avi appear +352 s after PRINT_DONE and grow until +370 s. 420 s
            # covers that worst case with margin; syncing too early would copy
            # a half-written file.
            "auto_sync_delay": 420,
            "auto_sync_action": "copy",
            # TEMP: enable to log how long the A1 mini takes to render its
            # timelapse after PRINT_DONE (plan §10.8 auto_sync_delay tuning).
            "auto_sync_measure": False,
            # Map of SD-card video name -> real "YYYY-MM-DD HH:MM" print-end
            # time, captured from OctoPrint's own PrintDone event. The only
            # trustworthy date source for uncopied videos: the A1 mini stamps
            # everything on the SD card (name, MDTM, thumbnail, logs) with a
            # wrong camera-subsystem clock in LAN-only mode, and nothing on the
            # card links a video to its real time. See the date note in
            # docs/reference/configuration.md.
            "print_dates": {},
        }

    def get_settings_restricted_paths(self):
        return {"admin": [["access_code"]]}

    def on_settings_save(self, data):
        old = {key: self._settings.get([key]) for key in DAEMON_SETTINGS}
        result = octoprint.plugin.SettingsPlugin.on_settings_save(self, data)
        new = {key: self._settings.get([key]) for key in DAEMON_SETTINGS}

        if old == new:
            return result

        if self._manager is None:
            self._logger.error("webcamd manager not initialized")
            return result
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
            {
                "type": "tab",
                "name": "BambuCam Timelapse",
                "template": "bambucam_tab.jinja2",
                "custom_bindings": True,
            },
        ]

    def get_assets(self):
        return {
            "js": ["js/BambuCam.js"],
            "css": ["css/BambuCam.css"],
        }

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

    def get_api_commands(self) -> dict:
        return {
            "restart": [],
            "test_connection": ["hostname", "access_code"],
            "detect_connector": [],
            "ffmpeg_status": [],
            "set_led": ["on"],
            "led_monitor_start": [],
            "led_monitor_stop": [],
            "fetch_info": [],
            "list_timelapses": [],
            "copy_timelapses": ["names"],
            "move_timelapses": ["names"],
            "delete_timelapses": ["names"],
            "list_local_avi": [],
            "convert_local_avi": ["names"],
        }

    def is_api_protected(self) -> bool:
        return True

    def on_api_get(self, request) -> flask.Response:
        if not Permissions.SETTINGS.can():
            flask.abort(403)
        thumb_name = request.args.get("thumb")
        if thumb_name:
            return self._handle_thumbnail(thumb_name)
        if self._manager is None:
            flask.abort(500)
        status = self._manager.status()
        status["stream_url"] = self._stream_url()
        status["led_available"] = bool(self._effective_serial())
        connector_state = connector_led.current_state(
            self._printer, self._logger
        )
        status["led_on"] = (
            connector_state if connector_state is not None else self._led_state
        )
        return flask.jsonify(status)

    def _handle_thumbnail(self, name) -> flask.Response:
        """Return the SD-card preview JPEG for ``name``, disk-cached.

        Thumbnails are fetched **serialized** over the printer's single FTPS
        connection (the browser requests them all at once, which otherwise
        triggers ``425 Can't open data connection``) and cached on disk so a
        refresh doesn't re-hit the printer. A served-from-cache hit needs no FTP
        at all.
        """
        cache_path = self._thumb_cache_path(name)
        if cache_path and os.path.exists(cache_path):
            return self._jpeg_response(cache_path)
        if cache_path is None:
            flask.abort(404)

        def probe():
            with self._thumb_lock:
                if os.path.exists(cache_path):
                    return
                try:
                    with self._make_ftp() as svc:
                        data = svc.fetch_thumbnail(name)
                    if data:
                        self._write_thumb_cache(cache_path, data)
                except FtpError as exc:
                    self._logger.debug("thumbnail unavailable: %s", exc.reason)
                except Exception:  # noqa: BLE001
                    self._logger.exception("thumbnail fetch failed")

        t = threading.Thread(target=probe, daemon=True)
        t.start()
        t.join(timeout=30)
        if not os.path.exists(cache_path):
            flask.abort(404)
        return self._jpeg_response(cache_path)

    def _thumb_cache_dir(self) -> str:
        return os.path.join(self.get_plugin_data_folder(), "thumb_cache")

    def _thumb_cache_path(self, name) -> Optional[str]:
        """Safe, contained cache path for ``name``'s thumbnail, or None."""
        safe = sanitize_filename(os.path.basename(name))
        if not safe:
            return None
        stem = os.path.splitext(safe)[0]
        cache_dir = self._thumb_cache_dir()
        path = os.path.join(cache_dir, stem + ".jpg")
        if not self._is_contained(path, cache_dir):
            return None
        return path

    def _write_thumb_cache(self, path, data) -> None:
        # re-assert containment at the sink (path already vetted by
        # _thumb_cache_path; this guards against future callers and makes
        # the sanitization visible to taint analysis)
        cache_dir = self._thumb_cache_dir()
        if not self._is_contained(path, cache_dir):
            return
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".part"
            with open(tmp, "wb") as fh:
                fh.write(data)
            os.replace(tmp, path)
        except OSError:
            self._logger.warning("could not cache thumbnail %s", path)

    def _jpeg_response(self, path) -> flask.Response:
        if not self._is_contained(path, self._thumb_cache_dir()):
            flask.abort(404)
        with open(path, "rb") as fh:
            data = fh.read()
        resp = flask.Response(data, mimetype="image/jpeg")
        resp.headers["Cache-Control"] = "private, max-age=86400"
        return resp

    def on_api_command(self, command, data) -> Optional[flask.Response]:
        if command == "detect_connector":
            if not Permissions.SETTINGS.can():
                flask.abort(403)
            return flask.jsonify(
                ok=True, connector=self._detect_connector().as_dict()
            )

        if command == "ffmpeg_status":
            if not Permissions.SETTINGS.can():
                flask.abort(403)
            return flask.jsonify(
                ok=True, ffmpeg=self._make_transcoder().status()
            )

        # fetch_info only reads (and the password is already redacted by the
        # vendored webcam.py), so SETTINGS is enough; the rest needs ADMIN.
        if self._manager is None:
            flask.abort(500)
        if command == "fetch_info":
            if not Permissions.SETTINGS.can():
                flask.abort(403)
            info = self._manager.fetch_info()
            if info is None:
                return flask.jsonify(ok=False, reason="unreachable")
            return flask.jsonify(ok=True, info=info)

        if command == "set_led":
            if not Permissions.CONTROL.can():
                flask.abort(403)
            return self._handle_set_led(bool(data.get("on")))

        if command == "led_monitor_start":
            if not Permissions.SETTINGS.can():
                flask.abort(403)
            return self._handle_led_monitor_start()

        if command == "led_monitor_stop":
            if not Permissions.SETTINGS.can():
                flask.abort(403)
            self._stop_led_monitor()
            return flask.jsonify(ok=True)

        if command == "list_timelapses":
            if not Permissions.SETTINGS.can():
                flask.abort(403)
            return self._handle_list_timelapses()

        if command == "list_local_avi":
            if not Permissions.SETTINGS.can():
                flask.abort(403)
            return flask.jsonify(ok=True, files=self._list_local_avi())

        if not Permissions.ADMIN.can():
            flask.abort(403)

        if command in (
            "copy_timelapses",
            "move_timelapses",
            "delete_timelapses",
        ):
            op = {
                "copy_timelapses": "copy",
                "move_timelapses": "move",
                "delete_timelapses": "delete",
            }[command]
            names = data.get("names") or []
            return self._handle_timelapse_op(op, names)

        if command == "convert_local_avi":
            names = data.get("names") or []
            return self._handle_convert_local(names)

        if command == "restart":
            ok, error = self._manager.restart(self._daemon_config())
            return flask.jsonify(ok=ok, error=error)

        if command == "test_connection":
            result = {}
            done = threading.Event()
            host = data["hostname"]
            code = data["access_code"]
            if not code:
                host, code = self._effective_credentials()

            def probe():
                ok, reason = WebcamdManager.test_connection(host, code)
                result["ok"] = ok
                result["reason"] = reason
                done.set()

            threading.Thread(target=probe, daemon=True).start()
            if not done.wait(timeout=12):
                return flask.jsonify(ok=False, reason="timeout")
            return flask.jsonify(**result)

    def _detect_connector(self) -> bambu_connector.ConnectorInfo:
        """Best-effort probe of OctoPrint-BambuConnector's connection data."""
        return bambu_connector.detect(self._plugin_manager, self._settings)

    def _effective_credentials(self):
        """Return the ``(hostname, access_code)`` to use for FTPS/webcamd.

        In ``auto`` mode, prefer the values OctoPrint-BambuConnector already
        knows (so the user does not type them twice); fall back to the manual
        fields whenever the connector is unavailable or incomplete.
        """
        hostname = self._settings.get(["hostname"])
        access_code = self._settings.get(["access_code"])
        if self._settings.get(["config_source"]) == "auto":
            info = self._detect_connector()
            if info.available:
                hostname = info.hostname
                access_code = info.access_code
        return str(hostname or ""), str(access_code or "")

    def _make_ftp(self) -> BambuTimelapseFtp:
        """Build a service from the effective printer credentials."""
        hostname, access_code = self._effective_credentials()
        return BambuTimelapseFtp(self._logger, hostname, access_code)

    def _effective_serial(self) -> str:
        """Return the printer serial for MQTT, or ``""`` if unknown.

        The serial is only available from OctoPrint-BambuConnector's
        connection profile; without it LED control cannot work, so the button
        stays hidden in the UI.
        """
        return str(self._detect_connector().serial or "")

    def _make_mqtt(self) -> BambuMqttClient:
        """Build an MQTT client from the effective credentials + serial."""
        hostname, access_code = self._effective_credentials()
        serial = self._effective_serial()
        return BambuMqttClient(self._logger, hostname, access_code, serial)

    def _handle_set_led(self, on: bool) -> flask.Response:
        """Toggle the printer LED over MQTT, threaded and time-capped.

        Only one LED command runs at a time: the printer's MQTT broker tolerates
        very few concurrent connections, so a second request while one is in
        flight is rejected with ``busy`` rather than opening another socket.
        """
        with self._led_lock:
            if self._led_busy:
                return flask.jsonify(ok=False, reason="busy")
            self._led_busy = True

        result: dict = {}
        done = threading.Event()

        def probe():
            try:
                if connector_led.set_chamber_light(
                    self._printer, on, self._logger
                ):
                    pass
                else:
                    monitor = self._led_monitor
                    if monitor is not None:
                        monitor.set_chamber_light(on)
                    else:
                        self._make_mqtt().set_chamber_light(on)
                result["ok"] = True
            except MqttError as exc:
                result["ok"] = False
                result["reason"] = exc.reason
            except Exception:  # noqa: BLE001 - never leak internals to client
                self._logger.exception("LED command failed")
                result["ok"] = False
                result["reason"] = "error"
            finally:
                with self._led_lock:
                    self._led_busy = False
                done.set()

        threading.Thread(target=probe, daemon=True).start()
        if not done.wait(timeout=20):
            return flask.jsonify(ok=False, reason="timeout")
        return flask.jsonify(**result)

    def _handle_led_monitor_start(self) -> flask.Response:
        """Open the standing light-state monitor (idempotent).

        Started when the webcam tab becomes visible. Returns the current known
        state so the UI can sync immediately; ``led_on`` is ``None`` until the
        printer's first report arrives.
        """
        if self._led_monitor is not None:
            return flask.jsonify(ok=True, led_on=self._led_state)
        if connector_led.available(self._printer, self._logger):
            return flask.jsonify(
                ok=True,
                led_on=connector_led.current_state(self._printer, self._logger),
            )
        serial = self._effective_serial()
        if not serial:
            return flask.jsonify(ok=False, reason="no_serial")
        hostname, access_code = self._effective_credentials()
        monitor = BambuMqttMonitor(
            self._logger,
            hostname,
            access_code,
            serial,
            on_change=self._on_led_state_change,
        )
        try:
            monitor.start()
        except MqttError as exc:
            return flask.jsonify(ok=False, reason=exc.reason)
        self._led_monitor = monitor
        return flask.jsonify(ok=True, led_on=self._led_state)

    def _stop_led_monitor(self) -> None:
        """Tear down the light-state monitor if running. Idempotent."""
        monitor, self._led_monitor = self._led_monitor, None
        if monitor is not None:
            monitor.stop()

    def _on_led_state_change(self, on) -> None:
        """Monitor callback: cache the state and push it to the browser."""
        self._led_state = on
        try:
            self._plugin_manager.send_plugin_message(
                self._identifier, {"type": "led_state", "on": on}
            )
        except Exception:  # noqa: BLE001 - never let a push kill the MQTT loop
            self._logger.exception("could not push LED state")

    def _handle_list_timelapses(self) -> flask.Response:
        """Threaded FTP listing, capped like ``test_connection``.

        Returns ``{ok: True, files: [{name, size, date, copied}]}`` or
        ``{ok: False, reason}``.
        """
        result: dict = {}
        done = threading.Event()

        def probe():
            try:
                with self._make_ftp() as svc:
                    files = svc.list_timelapses()
                print_dates = self._settings.get(["print_dates"]) or {}
                for f in files:
                    local = self._local_copy_name(f["name"])
                    f["copied"] = local is not None
                    if local is not None and local != f["name"]:
                        f["renamed"] = local
                    # Date sources, best first. The A1 mini stamps everything
                    # on the SD card (name, MDTM, thumbnail) with a wrong
                    # camera-subsystem clock in LAN-only mode, so the raw date
                    # is untrustworthy. We override it with a real date when
                    # we have one:
                    #   1. copied file -> its real local mtime (exact)
                    #   2. otherwise   -> the PrintDone time we recorded for
                    #      this video name (captured from OctoPrint's event)
                    # If neither exists, we flag the raw date as unreliable so
                    # the UI can mark it instead of pretending it is correct.
                    real_date = (
                        self._local_copy_date(local)
                        if local is not None
                        else None
                    )
                    if real_date is None:
                        real_date = print_dates.get(f["name"])
                    if real_date is not None:
                        f["date"] = real_date
                        f["date_corrected"] = True
                    else:
                        f["date_unreliable"] = True
                result["ok"] = True
                result["files"] = files
            except FtpError as exc:
                result["ok"] = False
                result["reason"] = exc.reason
            except Exception:  # noqa: BLE001 - never leak internals to client
                self._logger.exception("timelapse list failed")
                result["ok"] = False
                result["reason"] = "error"
            finally:
                done.set()

        threading.Thread(target=probe, daemon=True).start()
        if not done.wait(timeout=30):
            return flask.jsonify(ok=False, reason="timeout")
        return flask.jsonify(**result)

    def _local_copy_date(self, local_name):
        """Real ``"YYYY-MM-DD HH:MM"`` date of a copied file, or ``None``.

        Uses the local file's mtime, which we set to the real copy time (the
        camera SD date is unreliable in LAN-only mode). This is the exact date
        for anything already pulled, so it beats the estimated offset.
        """
        if not local_name:
            return None
        basefolder = self._settings.global_get_basefolder("timelapse")
        path = os.path.join(basefolder, local_name)
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return None
        return datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")

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

    def get_timelapse_extensions(self):
        """Teach OctoPrint's native Timelapse tab to also list ``.avi`` files.

        Bambu P1/A1 printers record timelapses as ``.avi`` (alongside ``.mp4``
        on some firmware). OctoPrint's built-in tab only recognizes
        ``mpg/mpeg/mp4/m4v/mkv`` (see ``octoprint.timelapse._extensions``), so a
        copied ``.avi`` would land in the folder but never appear. Registering
        this ``octoprint.timelapse.extensions`` hook adds ``avi`` to the
        allow-list. ``.mp4`` is already covered upstream.
        """
        return ["avi"]

    def _daemon_config(self):
        hostname, access_code = self._effective_credentials()
        return {
            "hostname": hostname,
            "access_code": access_code,
            "port": self._settings.get_int(["port"]),
            "bind_address": self._settings.get(["bind_address"]),
            "override_resolution": self._settings.get_boolean(
                ["override_resolution"]
            ),
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
__plugin_version__ = _PLUGIN_VERSION
__plugin_author__ = "Ajimaru"
__plugin_url__ = "https://github.com/Ajimaru/OctoPrint-BambuCam"
__plugin_description__ = "Bambu Lab camera stream integration for OctoPrint"
__plugin_license__ = "AGPL-3.0-or-later"
__plugin_pythoncompat__ = ">=3.9,<4"
__plugin_implementation__ = BambucamPlugin()
__plugin_hooks__ = {
    "octoprint.plugin.softwareupdate.check_config": (
        __plugin_implementation__.get_update_information
    ),
    "octoprint.timelapse.extensions": (
        __plugin_implementation__.get_timelapse_extensions
    ),
}
