"""BambuCam OctoPrint plugin.

Provides a webcam stream and snapshot endpoint for Bambu Lab printers by
managing a ``webcamd`` daemon and exposing it through OctoPrint's webcam,
settings, template and simple-API plugin mixins.
"""

import contextlib
import datetime
import logging
import logging.handlers
import os
import threading
import urllib.parse
import urllib.request
from typing import TYPE_CHECKING, Optional

import flask
import octoprint.plugin
from octoprint.access.permissions import Permissions
from octoprint.schema.webcam import RatioEnum, Webcam, WebcamCompatibility
from octoprint.webcams import WebcamNotAbleToTakeSnapshotException

from . import bambu_connector, connector_led, render_presets
from ._version import VERSION as _PLUGIN_VERSION
from .autosync import AutoSyncMixin
from .daemon import WebcamdManager
from .ftp import BambuTimelapseFtp, FtpError
from .ipcam_ftp import BambuIpcamFtp
from .ipcam_sync import IpcamSyncMixin
from .mqtt import BambuMqttClient, BambuMqttMonitor, MqttError
from .paths import sanitize_filename
from .raw_files_ops import RawFilesOpsMixin
from .render_paths import (
    RAW_CHUNKS_DIRNAME,
    RENDER_ROOT,
    TRASH_DIRNAME,
    WORK_DIRNAME,
)
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
    IpcamSyncMixin,
    RawFilesOpsMixin,
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
        self._init_ipcam_sync()
        self._init_raw_files()

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
                # don't create the log file at plugin init; only once the
                # first HTTP request line is actually logged (--loghttp on)
                delay=True,
            )
            handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
            logger.addHandler(handler)
        return logger

    def on_after_startup(self):
        # The render pipeline is independent of the webcamd daemon: recover
        # interrupted render jobs and start the retention sweep even when the
        # camera itself is disabled or unconfigured. It must never block the
        # daemon start, so failures are logged, not raised.
        try:
            self.start_render_pipeline()
        except Exception:  # noqa: BLE001  # pylint: disable=broad-except
            self._logger.exception("render pipeline startup failed")
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
        self.stop_render_pipeline()
        self._stop_led_monitor()
        if self._manager is not None:
            self._manager.stop()

    def on_event(self, event, payload) -> None:
        """Fan a printer event out to both post-print pipelines.

        ``AutoSyncMixin.on_event`` drives the render-gate flags and the serial
        post-print pipeline; ``on_ipcam_event`` snapshots the ``/ipcam``
        baseline at ``PrintStarted`` for pipeline stage 3.
        """
        AutoSyncMixin.on_event(self, event, payload)
        self.on_ipcam_event(event, payload)

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
            # Prefix downloaded timelapses with the print job's file name
            # (from the print_jobs map below), e.g.
            # ``benchy.gcode.3mf_video_2026-05-18_08-07-44.mp4``.
            "prefix_job_name": True,
            "transcode_to_mp4": True,
            # Use the print job's gcode preview image (from Bambu Connector)
            # as the SD-timelapse thumbnail instead of a video frame.
            "sd_thumb_from_gcode": False,
            "auto_sync": False,
            # The A1 mini usually finishes rendering its timelapse *during*
            # the print, but not always: a measurement (plan §10.8) saw the
            # .avi appear +352 s after PRINT_DONE and grow until +370 s. 420 s
            # covers that worst case with margin; syncing too early would copy
            # a half-written file.
            "auto_sync_delay": 420,
            "auto_sync_action": "copy",
            # Map of SD-card video name -> real "YYYY-MM-DD HH:MM" print-end
            # time, captured from OctoPrint's own PrintDone event. The only
            # trustworthy date source for uncopied videos: the A1 mini stamps
            # everything on the SD card (name, MDTM, thumbnail, logs) with a
            # wrong camera-subsystem clock in LAN-only mode, and nothing on the
            # card links a video to its real time. See the date note in
            # docs/reference/configuration.md.
            "print_dates": {},
            # Map of SD-card video name -> gcode job name, captured with the
            # print date. Labels manual /ipcam harvests with the real print.
            "print_jobs": {},
            # ── Raw Files render pipeline (plan §3) ────────────────────────
            "render_enabled": True,
            # Hides the "Raw Files" subtab (cosmetic only — the render
            # pipeline itself keeps running while this is off).
            "render_tab_visible": True,
            "auto_download_ipcam": False,
            "auto_render_new_groups": False,
            "render_only_when_idle": True,
            "default_preset": render_presets.DEFAULT_PRESET,
            # Empty = fall back to OctoPrint's webcam.ffmpeg (and its
            # sibling ffprobe) — an override is only for exotic installs.
            "ffmpeg_path": "",
            "ffprobe_path": "",
            # 1 keeps a Raspberry Pi responsive; 0 = all cores.
            "ffmpeg_threads": 1,
            # 0 = use the transcoder's default timeout.
            "render_timeout": 0,
            "max_queue_size": 10,
            # Reclaim a crashed render's lockfile after this many seconds.
            "stale_lock_timeout": 86400,
            # 0 = keep rendered groups' chunks forever.
            "chunks_retention_days": 0,
            "move_to_trash": True,
            "raw_thumb_from_gcode": False,
        }

    def get_settings_restricted_paths(self):
        return {"admin": [["access_code"]]}

    # Settings keys that were dropped from the defaults but stay in an
    # existing config.yaml, because OctoPrint persists what was once saved and
    # never prunes it. Both backed checkboxes that no code ever read (removed
    # in fd44a13 as "never-implemented"); the startup recovery they suggested
    # now runs unconditionally in RawFilesOpsMixin.start_render_pipeline.
    _OBSOLETE_SETTINGS = ("use_lockfiles", "recover_on_startup")

    def get_settings_version(self):
        return 1

    def on_settings_migrate(self, target, current):
        """Drop settings that no longer back anything (OctoPrint hook).

        ``current`` is ``None`` for a config written before this plugin had a
        settings version — which is every install carrying the dead keys.
        """
        if current is not None and current >= target:
            return
        for key in self._OBSOLETE_SETTINGS:
            if self._settings.get([key]) is None:
                continue
            self._settings.remove([key])
            self._logger.info("settings: removed obsolete key %r", key)

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
            # One tab holding both workflows as subtabs: the SD-card
            # timelapses and the /ipcam raw footage. ``bambucam_raw.jinja2``
            # is included by ``bambucam_tab.jinja2`` rather than registered on
            # its own — a second registration would render the same template
            # twice, and the copy outside this tab would get no view model
            # (the bindings target ``#tab_plugin_bambucam``).
            {
                "type": "tab",
                "name": "BambuCam",
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
        url = self._loopback_url("snapshot")
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "http" or parsed.hostname != "127.0.0.1":
            raise WebcamNotAbleToTakeSnapshotException(self._webcam_name)
        with urllib.request.urlopen(  # nosec B310 - scheme/host checked above
            url, timeout=10
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
            # Raw Files render pipeline. Payload fields are validated in the
            # handlers (bad_id/bad_jobid/... responses) rather than declared
            # required here, so the UI gets consistent JSON errors instead of
            # OctoPrint's generic 400.
            "list_raw_footage": [],
            "scan_raw": [],
            "render_status": [],
            "ffprobe_status": [],
            "render_ffmpeg_status": [],
            "pipeline_status": [],
            "harvest_ipcam": [],
            "cancel_harvest": [],
            "start_render": [],
            "cancel_render": [],
            "delete_group": [],
            "delete_chunks": [],
        }

    def is_api_protected(self) -> bool:
        return True

    def on_api_get(self, request) -> flask.Response:
        if not Permissions.SETTINGS.can():
            flask.abort(403)
        raw_thumb = request.args.get("raw_thumb")
        if raw_thumb:
            return self.handle_raw_thumb(raw_thumb)
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

    # See _dispatch_raw_pipeline_command for the permission split.
    _RAW_READ_COMMANDS = {
        "list_raw_footage": "handle_list_raw_footage",
        "scan_raw": "handle_scan_raw",
        "render_status": "handle_render_status",
        "ffprobe_status": "handle_ffprobe_status",
        "render_ffmpeg_status": "handle_render_ffmpeg_status",
        "pipeline_status": "handle_pipeline_status",
    }
    _RAW_ADMIN_COMMANDS = {
        "harvest_ipcam": "handle_harvest_ipcam",
        "cancel_harvest": "handle_cancel_harvest",
        "start_render": "handle_start_render",
        "cancel_render": "handle_cancel_render",
        "delete_group": "handle_delete_group",
        "delete_chunks": "handle_delete_chunks",
    }

    # Zero-arg, SETTINGS-permission commands that do not need the webcamd
    # manager, dispatched via _dispatch_settings_command.
    _SETTINGS_COMMANDS = ("detect_connector", "ffmpeg_status")

    # Zero-arg, SETTINGS-permission commands that require the webcamd
    # manager to be initialized, also dispatched via
    # _dispatch_settings_command.
    _MANAGER_SETTINGS_COMMANDS = (
        "fetch_info",
        "led_monitor_start",
        "led_monitor_stop",
        "list_timelapses",
        "list_local_avi",
    )

    # ADMIN-only commands taking the raw ``data`` payload.
    _TIMELAPSE_OPS = {
        "copy_timelapses": "copy",
        "move_timelapses": "move",
        "delete_timelapses": "delete",
    }

    def on_api_command(self, command, data) -> Optional[flask.Response]:
        response = self._dispatch_non_admin_command(command, data)
        if response is not None:
            return response
        return self._dispatch_admin_command(command, data)

    def _dispatch_non_admin_command(
        self, command, data
    ) -> Optional[flask.Response]:
        """Commands with their own (non-ADMIN) permission check.

        Returns ``None`` for any command it doesn't own, so the caller falls
        through to the ADMIN-gated dispatch.
        """
        response = self._dispatch_raw_pipeline_command(command, data)
        if response is not None:
            return response

        if command == "set_led":
            if not Permissions.CONTROL.can():
                flask.abort(403)
            return self._handle_set_led(bool(data.get("on")))

        if command in self._SETTINGS_COMMANDS:
            if not Permissions.SETTINGS.can():
                flask.abort(403)
            return self._dispatch_settings_command(command)

        if command in self._MANAGER_SETTINGS_COMMANDS:
            if not Permissions.SETTINGS.can():
                flask.abort(403)
            if self._manager is None:
                flask.abort(500)
            return self._dispatch_settings_command(command)

        return None

    def _dispatch_raw_pipeline_command(
        self, command, data
    ) -> Optional[flask.Response]:
        """Raw Files pipeline commands, split by required permission.

        Reads need SETTINGS (like the timelapse listing); anything that
        transfers, renders or deletes needs ADMIN (like the SD-card batch
        operations). These work without the webcamd manager.
        """
        if command in self._RAW_READ_COMMANDS:
            if not Permissions.SETTINGS.can():
                flask.abort(403)
            return getattr(self, self._RAW_READ_COMMANDS[command])()

        if command in self._RAW_ADMIN_COMMANDS:
            if not Permissions.ADMIN.can():
                flask.abort(403)
            return getattr(self, self._RAW_ADMIN_COMMANDS[command])(data)

        return None

    def _dispatch_admin_command(
        self, command, data
    ) -> Optional[flask.Response]:
        """Remaining commands, all requiring ADMIN permission."""
        if not Permissions.ADMIN.can():
            flask.abort(403)

        if command in self._TIMELAPSE_OPS:
            names = data.get("names") or []
            return self._handle_timelapse_op(
                self._TIMELAPSE_OPS[command], names
            )

        if command == "convert_local_avi":
            names = data.get("names") or []
            return self._handle_convert_local(names)

        if self._manager is None:
            flask.abort(500)

        if command == "restart":
            ok, error = self._manager.restart(self._daemon_config())
            return flask.jsonify(ok=ok, error=error)

        if command == "test_connection":
            return self._handle_test_connection(data)

        return None

    def _dispatch_settings_command(self, command) -> flask.Response:
        """Zero-arg SETTINGS-permission commands (manager already checked
        for those that need it)."""
        if command == "detect_connector":
            return flask.jsonify(
                ok=True, connector=self._detect_connector().as_dict()
            )
        if command == "ffmpeg_status":
            return flask.jsonify(
                ok=True, ffmpeg=self._make_transcoder().status()
            )
        if command == "fetch_info":
            if self._manager is None:  # caller guarantees; belt-and-braces
                flask.abort(500)
            info = self._manager.fetch_info()
            if info is None:
                return flask.jsonify(ok=False, reason="unreachable")
            return flask.jsonify(ok=True, info=info)
        if command == "led_monitor_start":
            return self._handle_led_monitor_start()
        if command == "led_monitor_stop":
            self._stop_led_monitor()
            return flask.jsonify(ok=True)
        if command == "list_timelapses":
            return self._handle_list_timelapses()
        return flask.jsonify(ok=True, files=self._list_local_avi())

    def _handle_test_connection(self, data) -> flask.Response:
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

    def _make_ipcam_ftp(self) -> BambuIpcamFtp:
        """Build an ``/ipcam`` service from the effective credentials."""
        hostname, access_code = self._effective_credentials()
        return BambuIpcamFtp(self._logger, hostname, access_code)

    @contextlib.contextmanager
    def _webcam_paused_for_harvest(self):
        """Stop the live stream while ``/ipcam`` chunks are pulled.

        The printer serves FTPS at ~180 KB/s and the stream competes for the
        same camera subsystem, so the daemon is stopped for the pull and
        restarted afterwards. Best-effort in both directions — a pause or
        resume failure must never break the harvest itself.
        """
        manager = self._manager
        paused_manager: Optional[WebcamdManager] = None
        if manager is not None and self._settings.get_boolean(["enabled"]):
            try:
                manager.stop()
                paused_manager = manager
                self._logger.info("webcamd paused for ipcam harvest")
            except OSError:
                self._logger.exception("could not pause webcamd for harvest")
        try:
            yield
        finally:
            if paused_manager is not None:
                try:
                    ok, error = paused_manager.start(self._daemon_config())
                    if not ok:
                        self._logger.error(
                            "could not resume webcamd after harvest: %s",
                            error,
                        )
                except OSError:
                    self._logger.exception(
                        "could not resume webcamd after harvest"
                    )

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
            # never leak internals to the client
            except Exception:  # noqa: BLE001  # pylint: disable=broad-except
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
        # never let a push kill the MQTT loop
        except Exception:  # noqa: BLE001  # pylint: disable=broad-except
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

    def get_additional_backup_excludes(self, excludes, *args, **kwargs):
        """Exclude bulky render dirs from OctoPrint backups.

        Chunks are re-harvestable from the printer's ``/ipcam`` folder and
        the work/trash dirs are transient, so backing them up only bloats
        the archive. When the user excludes "timelapse" from the backup the
        rendered videos are gone anyway, so the whole render tree (incl.
        thumbs/metadata) is dropped for consistency.
        """
        if "timelapse" in (excludes or []):
            return [RENDER_ROOT]
        return [
            os.path.join(RENDER_ROOT, RAW_CHUNKS_DIRNAME),
            os.path.join(RENDER_ROOT, WORK_DIRNAME),
            os.path.join(RENDER_ROOT, TRASH_DIRNAME),
        ]

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
    "octoprint.plugin.backup.additional_excludes": (
        __plugin_implementation__.get_additional_backup_excludes
    ),
}
