"""Process manager for the vendored webcamd-bambu MJPEG daemon.

Runs ``vendor/webcamd_bambu/webcam.py`` as a supervised child process:
start/stop/restart, stdout/stderr log pumping into the plugin logger, and a
watchdog that restarts the daemon with exponential backoff after unexpected
exits. The daemon's working directory is the vendor directory so that the
``--showfps`` watermark can resolve its bundled TTF font via a relative path.
"""

import ipaddress
import json
import os
import re
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

VENDOR_DIR = os.path.join(os.path.dirname(__file__), "vendor", "webcamd_bambu")
WEBCAM_SCRIPT = os.path.join(VENDOR_DIR, "webcam.py")

# exponential backoff after unexpected exits, capped
_BACKOFF_INITIAL = 2.0
_BACKOFF_MAX = 60.0

# webcam.py's --loghttp lines look like "<ISO datetime>: <ip> <message>"
# (see WebRequestHandler.log_message); match that prefix to route them to the
# dedicated HTTP log file instead of the main plugin log.
_HTTP_LOG_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)?: ")


class WebcamdManager:
    """Supervise the vendored ``webcamd`` child process.

    Handles start/stop/restart, pumps the child's output into the plugin
    logger, and runs a generation-tracked watchdog that restarts the daemon
    with exponential backoff after unexpected exits while guaranteeing only one
    daemon ever runs at a time.
    """

    def __init__(self, logger, on_state_change=None, http_logger=None):
        """
        :param logger: plugin logger; child output is forwarded here
        :param on_state_change: optional callable(state: str, detail: dict)
               called on "started", "stopped", "crashed", "gave_up"
        :param http_logger: optional logger for the daemon's --loghttp output;
               those lines are routed here instead of the main plugin log
        """
        self._logger = logger
        self._on_state_change = on_state_change
        self._http_logger = http_logger

        self._process = None
        self._lock = threading.RLock()
        self._stop_requested = False
        self._started_at = None
        self._restart_count = 0
        self._restart_timestamps = []
        self._watchdog_thread = None
        self._last_error = None
        # bumped on every stop()/spawn() so a stale watchdog from a previous
        # process generation can detect it has been superseded and bail out,
        # preventing two daemons from ever running in parallel
        self._generation = 0

        self._config = {}

    # ── public API ────────────────────────────────────────────────────────

    def start(self, config):
        """Start the daemon with the given config dict (see plugin settings).

        Returns (ok, error_message)."""
        # Tear down any existing process *outside* the lock (stop() blocks on
        # process.wait()); this guarantees we never run two daemons in
        # parallel, e.g. when the port changed and a previous instance is
        # still alive.
        self.stop()

        with self._lock:
            self._stop_requested = False
            self._config = dict(config)
            self._last_error = None

            error = self._validate(self._config)
            if error:
                self._last_error = error
                return False, error

            ok, error = self._spawn()
            if not ok:
                self._last_error = error
                return False, error

            return True, None

    def stop(self):
        """Terminate the running daemon (if any) and supersede its watchdog."""
        with self._lock:
            self._stop_requested = True
            self._generation += 1  # supersede any running watchdog
            process = self._process
            self._process = None

        if process is None or process.poll() is not None:
            return

        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._logger.warning("webcamd did not exit on SIGTERM, killing it")
            process.kill()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._logger.error("webcamd could not be killed")
        self._logger.info("webcamd stopped")
        self._notify("stopped", {})

    def restart(self, config=None):
        """Stop and start again, reusing the last config unless one is given.

        Returns (ok, error_message)."""
        self.stop()
        with self._lock:
            self._restart_timestamps = []
        return self.start(config if config is not None else self._config)

    def is_running(self):
        """Return True while the daemon process is alive."""
        process = self._process
        return process is not None and process.poll() is None

    def status(self):
        """Return a snapshot dict of daemon state for the API/frontend."""
        with self._lock:
            process = self._process
            running = process is not None and process.poll() is None
            return {
                "running": running,
                "pid": process.pid if running and process else None,
                "uptime": (
                    (time.monotonic() - self._started_at)
                    if running and self._started_at
                    else None
                ),
                "restarts": self._restart_count,
                "last_error": self._last_error,
                "info": self.fetch_info() if running else None,
            }

    def fetch_info(self):
        """GET /?info from the daemon (always via loopback).

        Returns a dict or None. The vendored daemon already redacts the access
        code, but strip it again here so the frontend can never receive it even
        if the vendor patch is lost in an upstream update."""
        port = self._config.get("port")
        if not port:
            return None
        # fixed http://127.0.0.1 loopback URL, not user-controlled
        url = f"http://127.0.0.1:{int(port)}/?info"
        try:
            opened = urllib.request.urlopen(url, timeout=3)  # nosec B310
            with opened as response:
                info = json.load(response)
            if isinstance(info.get("config"), dict):
                info["config"].pop("password", None)
            return info
        except (urllib.error.URLError, OSError, ValueError):
            return None

    @staticmethod
    def port_in_use(port, bind_address="127.0.0.1"):
        """Return True if the given port is already bound on the host."""
        # Probe a concrete address only. A wildcard bind_address ("0.0.0.0" or
        # "::") means "all interfaces"; we never probe-bind to the wildcard
        # itself — we fall back to loopback, which is enough to detect whether
        # the port is already taken on this host.
        try:
            is_wildcard = ipaddress.ip_address(bind_address).is_unspecified
        except ValueError:
            is_wildcard = False
        probe_address = "127.0.0.1" if is_wildcard else bind_address
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # webcam.py binds with allow_reuse_address (SO_REUSEADDR), so the probe
        # must too — otherwise a just-stopped daemon's socket lingering in
        # TIME_WAIT is reported as "in use" and a same-port restart is wrongly
        # rejected, even though webcam.py itself could rebind it fine.
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((probe_address, int(port)))
            return False
        except OSError:
            return True
        finally:
            probe.close()

    @staticmethod
    def test_connection(hostname, access_code, timeout=10.0):
        """Probe the printer camera port using the §2.2 handshake.

        Returns (ok, reason) with reason in
        ("ok", "unreachable", "auth_failed", "timeout", "error")."""
        username = "bblp"  # fixed Bambu LAN-mode username, not a secret
        auth_data = bytearray()
        auth_data += struct.pack("<I", 0x40)
        auth_data += struct.pack("<I", 0x3000)
        auth_data += struct.pack("<I", 0)
        auth_data += struct.pack("<I", 0)
        auth_data += username.encode("ascii").ljust(32, b"\x00")
        auth_data += access_code.encode("ascii").ljust(32, b"\x00")

        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        # Bambu LAN cameras present a self-signed cert and use a custom binary
        # handshake, so cert/hostname verification is intentionally disabled.
        # Floor the negotiated protocol at TLS 1.2 (printers support it) so the
        # legacy TLSv1/1.1 versions are never offered.
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        try:
            with socket.create_connection(
                (hostname, 6000), timeout=timeout
            ) as sock:
                with ctx.wrap_socket(sock, server_hostname=hostname) as tls:
                    tls.settimeout(timeout)
                    tls.write(auth_data)
                    # printer sends a 16-byte frame header when auth succeeds,
                    # closes the connection (empty read) when the code is wrong
                    data = tls.recv(16)
                    if len(data) == 0:
                        return False, "auth_failed"
                    return True, "ok"
        except socket.timeout:
            return False, "timeout"
        except (ConnectionRefusedError, OSError):
            return False, "unreachable"
        except Exception:
            return False, "error"

    # ── internals ─────────────────────────────────────────────────────────

    def _validate(self, config):
        if not config.get("hostname"):
            return "no printer hostname configured"
        if not config.get("access_code"):
            return "no access code configured"
        if not os.path.isfile(WEBCAM_SCRIPT):
            return f"vendored webcam.py not found at {WEBCAM_SCRIPT}"
        if self.port_in_use(
            config.get("port", 8181), config.get("bind_address", "127.0.0.1")
        ):
            return f"port {config.get('port')} is already in use"
        return None

    def _build_argv(self, config):
        argv = [
            sys.executable,
            # unbuffered child stdout so the log pump sees lines immediately
            "-u",
            WEBCAM_SCRIPT,
            "--hostname",
            str(config["hostname"]),
            "--password",
            str(config["access_code"]),
            "--port",
            str(config.get("port", 8181)),
            "--v4bindaddress",
            str(config.get("bind_address", "127.0.0.1")),
        ]
        width = config.get("width")
        if width:
            argv += ["--width", str(int(width))]
        height = config.get("height")
        if height:
            argv += ["--height", str(int(height))]
        rotate = int(config.get("rotate", -1))
        if rotate != -1:
            argv += ["--rotate", str(rotate)]
        encodewait = config.get("encodewait")
        if encodewait is not None:
            argv += ["--encodewait", str(encodewait)]
        if config.get("flashred"):
            argv.append("--flashred")
        if config.get("showfps"):
            argv.append("--showfps")
        if config.get("loghttp"):
            argv.append("--loghttp")
        return argv

    def _spawn(self):
        argv = self._build_argv(self._config)
        try:
            # cwd must be the vendor dir: webcam.py loads its watermark font
            # via a relative path
            process = subprocess.Popen(
                argv,
                cwd=VENDOR_DIR,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
        except OSError as exc:
            return False, f"failed to launch webcamd: {exc}"

        self._generation += 1
        generation = self._generation
        self._process = process
        self._started_at = time.monotonic()
        threading.Thread(
            target=self._pump_logs,
            args=(process,),
            name="bambucam-logpump",
            daemon=True,
        ).start()
        # one watchdog per spawn, tied to this generation; superseded watchdogs
        # exit instead of fighting over self._process
        self._watchdog_thread = threading.Thread(
            target=self._watchdog,
            args=(process, generation),
            name="bambucam-watchdog",
            daemon=True,
        )
        self._watchdog_thread.start()
        self._logger.info(
            "webcamd started (pid %d, port %s)",
            process.pid,
            self._config.get("port"),
        )
        self._notify("started", {"pid": process.pid})
        return True, None

    def _pump_logs(self, process):
        try:
            for line in process.stdout:
                line = line.rstrip()
                if not line:
                    continue
                # route --loghttp request lines to their own file (if enabled);
                # everything else (startup/errors) goes to the plugin log
                if self._http_logger is not None and _HTTP_LOG_RE.match(line):
                    self._http_logger.info(line)
                else:
                    self._logger.info("webcamd: %s", line)
        except Exception:
            pass

    def _watchdog(self, process, generation):
        """Supervise exactly the process from one spawn (identified by
        ``generation``). A newer generation means stop()/start() superseded us,
        so we exit immediately — only the newest watchdog ever acts, which is
        what stops parallel daemons from piling up."""
        backoff = _BACKOFF_INITIAL
        while True:
            # superseded by a newer spawn or an explicit stop → bow out
            if generation != self._generation:
                return

            returncode = process.poll()
            if returncode is None:
                # healthy run for over a minute resets the backoff
                uptime = time.monotonic() - (self._started_at or 0)
                if self._started_at and uptime > 60:
                    backoff = _BACKOFF_INITIAL
                time.sleep(1)
                continue

            # our process exited; claim the crash only if we're still current
            with self._lock:
                if generation != self._generation or self._stop_requested:
                    return
                self._process = None

            self._logger.warning(
                "webcamd exited unexpectedly with code %s", returncode
            )
            self._notify("crashed", {"returncode": returncode})

            if not self._config.get("autorestart", True):
                return

            now = time.monotonic()
            window = float(self._config.get("restart_window", 300))
            max_restarts = int(self._config.get("max_restarts", 5))
            self._restart_timestamps = [
                t for t in self._restart_timestamps if now - t < window
            ] + [now]
            if len(self._restart_timestamps) > max_restarts:
                self._last_error = (
                    f"webcamd crashed {len(self._restart_timestamps)} times "
                    f"within {int(window)} s — giving up; check hostname/"
                    "access code and restart manually"
                )
                self._logger.error(self._last_error)
                self._notify("gave_up", {"error": self._last_error})
                return

            self._logger.info("restarting webcamd in %.0f s", backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, _BACKOFF_MAX)

            with self._lock:
                # re-check: a manual restart/stop during the backoff sleep
                # would have advanced the generation; don't spawn a duplicate
                if generation != self._generation or self._stop_requested:
                    return
                self._restart_count += 1
                ok, error = self._spawn()
                if not ok:
                    self._last_error = error
                    self._logger.error("webcamd restart failed: %s", error)
                    return
                # _spawn started a fresh watchdog for the new generation; hand
                # supervision over to it and retire this one
                return

    def _notify(self, state, detail):
        if self._on_state_change is None:
            return
        try:
            self._on_state_change(state, detail)
        except Exception:
            self._logger.exception("state change callback failed")
