"""Tests for octoprint_bambucam.daemon.WebcamdManager."""

# pylint: disable=protected-access
# pylint: disable=import-outside-toplevel
# pylint: disable=use-implicit-booleaness-not-comparison

import json
import socket
import time
from unittest.mock import MagicMock, patch

from octoprint_bambucam.daemon import WEBCAM_SCRIPT, WebcamdManager

# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestInit:
    """WebcamdManager.__init__ sets sensible defaults."""

    def test_defaults(self, logger):
        """All internal state starts at zero/None/empty after construction."""
        m = WebcamdManager(logger)
        assert m._process is None
        assert m._generation == 0
        assert m._restart_count == 0
        assert m._stop_requested is False
        assert not m._config

    def test_callback_and_http_logger(self, logger):
        """Optional callback and http_logger are stored on the instance."""
        cb = MagicMock()
        http_log = MagicMock()
        m = WebcamdManager(logger, on_state_change=cb, http_logger=http_log)
        assert m._on_state_change is cb
        assert m._http_logger is http_log


# ---------------------------------------------------------------------------
# _notify
# ---------------------------------------------------------------------------


class TestNotify:
    """_notify forwards state-change events to the optional callback."""

    def test_no_callback(self, manager):
        """_notify without a callback must not raise."""
        manager._notify("started", {})

    def test_callback_called(self, manager_with_callback):
        """_notify invokes the callback with state and detail."""
        m, cb = manager_with_callback
        m._notify("started", {"pid": 42})
        cb.assert_called_once_with("started", {"pid": 42})

    def test_callback_exception_swallowed(self, manager_with_callback):
        """Exceptions raised by the callback must not propagate."""
        m, cb = manager_with_callback
        cb.side_effect = RuntimeError("boom")
        m._notify("started", {})


# ---------------------------------------------------------------------------
# port_in_use (static)
# ---------------------------------------------------------------------------


class TestPortInUse:
    """port_in_use probes whether a TCP port is already bound."""

    def test_free_port(self):
        """A port released before the check is reported as free."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        assert WebcamdManager.port_in_use(port, "127.0.0.1") is False

    def test_used_port(self):
        """A port still bound by a socket is reported as in use."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
            assert WebcamdManager.port_in_use(port, "127.0.0.1") is True

    def test_wildcard_probes_loopback(self):
        """bind_address 0.0.0.0 causes the probe to use 127.0.0.1."""
        with patch("socket.socket") as mock_sock_cls:
            mock_sock = MagicMock()
            mock_sock_cls.return_value = mock_sock
            mock_sock.bind.return_value = None
            result = WebcamdManager.port_in_use(8181, "0.0.0.0")
        mock_sock.bind.assert_called_once_with(("127.0.0.1", 8181))
        assert result is False

    def test_bind_raises_returns_true(self):
        """An OSError from bind means the port is in use."""
        with patch("socket.socket") as mock_sock_cls:
            mock_sock = MagicMock()
            mock_sock_cls.return_value = mock_sock
            mock_sock.bind.side_effect = OSError("already in use")
            result = WebcamdManager.port_in_use(8181, "127.0.0.1")
        assert result is True
        mock_sock.close.assert_called_once()

    def test_non_wildcard_bind_address_used(self):
        """A specific bind address is passed directly to the probe socket."""
        with patch("socket.socket") as mock_sock_cls:
            mock_sock = MagicMock()
            mock_sock_cls.return_value = mock_sock
            mock_sock.bind.return_value = None
            WebcamdManager.port_in_use(9000, "192.168.1.5")
        mock_sock.bind.assert_called_once_with(("192.168.1.5", 9000))


# ---------------------------------------------------------------------------
# test_connection (static)
# ---------------------------------------------------------------------------


class TestTestConnection:
    """test_connection performs the Bambu §2.2 SSL auth handshake."""

    def _make_tls_success(self):
        """Return a mock TLS socket that replies with a 16-byte auth frame."""
        mock_tls = MagicMock()
        mock_tls.recv.return_value = b"\x00" * 16
        mock_tls.__enter__ = MagicMock(return_value=mock_tls)
        mock_tls.__exit__ = MagicMock(return_value=False)
        return mock_tls

    def _make_sock_and_ctx(self, mock_tls):
        """Return a (mock_sock, mock_ctx) pair wrapping the given TLS mock."""
        mock_sock = MagicMock()
        mock_sock.__enter__ = MagicMock(return_value=mock_sock)
        mock_sock.__exit__ = MagicMock(return_value=False)
        mock_ctx = MagicMock()
        mock_ctx.wrap_socket.return_value = mock_tls
        return mock_sock, mock_ctx

    def test_success(self):
        """A 16-byte auth reply means the credentials are correct."""
        mock_tls = self._make_tls_success()
        mock_sock, mock_ctx = self._make_sock_and_ctx(mock_tls)

        with patch("socket.create_connection", return_value=mock_sock):
            with patch("ssl.SSLContext", return_value=mock_ctx):
                ok, reason = WebcamdManager.test_connection(
                    "printer.local", "12345678"
                )

        assert ok is True
        assert reason == "ok"

    def test_auth_failed_empty_response(self):
        """An empty recv reply means the printer rejected the access code."""
        mock_tls = MagicMock()
        mock_tls.recv.return_value = b""
        mock_tls.__enter__ = MagicMock(return_value=mock_tls)
        mock_tls.__exit__ = MagicMock(return_value=False)
        mock_sock, mock_ctx = self._make_sock_and_ctx(mock_tls)

        with patch("socket.create_connection", return_value=mock_sock):
            with patch("ssl.SSLContext", return_value=mock_ctx):
                ok, reason = WebcamdManager.test_connection(
                    "printer.local", "wrong"
                )

        assert ok is False
        assert reason == "auth_failed"

    def test_timeout(self):
        """A socket timeout is reported as 'timeout'."""
        with patch(
            "socket.create_connection",
            side_effect=socket.timeout("timed out"),
        ):
            ok, reason = WebcamdManager.test_connection("printer.local", "code")
        assert ok is False
        assert reason == "timeout"

    def test_connection_refused(self):
        """ConnectionRefusedError is reported as 'unreachable'."""
        with patch(
            "socket.create_connection",
            side_effect=ConnectionRefusedError(),
        ):
            ok, reason = WebcamdManager.test_connection("printer.local", "code")
        assert ok is False
        assert reason == "unreachable"

    def test_os_error(self):
        """A generic OSError (network down) is reported as 'unreachable'."""
        with patch(
            "socket.create_connection",
            side_effect=OSError("network down"),
        ):
            ok, reason = WebcamdManager.test_connection("printer.local", "code")
        assert ok is False
        assert reason == "unreachable"

    def test_unexpected_exception(self):
        """Any other exception falls back to the 'error' reason."""
        with patch(
            "socket.create_connection",
            side_effect=ValueError("weird"),
        ):
            ok, reason = WebcamdManager.test_connection("printer.local", "code")
        assert ok is False
        assert reason == "error"

    def test_auth_packet_contains_credentials(self):
        """Verify auth packet is built with correct username/access_code."""
        captured = {}

        mock_tls = MagicMock()
        mock_tls.recv.return_value = b"\x00" * 16
        mock_tls.__enter__ = MagicMock(return_value=mock_tls)
        mock_tls.__exit__ = MagicMock(return_value=False)

        def _capture_write(data):
            captured["auth"] = data

        mock_tls.write = _capture_write
        mock_sock, mock_ctx = self._make_sock_and_ctx(mock_tls)

        with patch("socket.create_connection", return_value=mock_sock):
            with patch("ssl.SSLContext", return_value=mock_ctx):
                WebcamdManager.test_connection("printer.local", "mycode")

        auth = captured.get("auth", b"")
        assert b"bblp" in auth
        assert b"mycode" in auth


# ---------------------------------------------------------------------------
# _validate
# ---------------------------------------------------------------------------


class TestValidate:
    """_validate rejects configs that are incomplete or cannot bind."""

    def test_no_hostname(self, manager, valid_config):
        """Missing hostname returns an error string."""
        cfg = dict(valid_config, hostname="")
        assert "hostname" in manager._validate(cfg)

    def test_no_access_code(self, manager, valid_config):
        """Missing access_code returns an error string."""
        cfg = dict(valid_config, access_code="")
        assert "access code" in manager._validate(cfg)

    def test_script_missing(self, manager, valid_config):
        """Missing webcam.py returns an error string."""
        with patch("os.path.isfile", return_value=False):
            result = manager._validate(valid_config)
        assert "webcam.py" in result

    def test_port_in_use(self, manager, valid_config):
        """An occupied port returns an error string."""
        with patch("os.path.isfile", return_value=True):
            with patch.object(WebcamdManager, "port_in_use", return_value=True):
                result = manager._validate(valid_config)
        assert "in use" in result

    def test_valid_config(self, manager, valid_config):
        """A fully valid config returns None (no error)."""
        with patch("os.path.isfile", return_value=True):
            with patch.object(
                WebcamdManager, "port_in_use", return_value=False
            ):
                result = manager._validate(valid_config)
        assert result is None


# ---------------------------------------------------------------------------
# _build_argv
# ---------------------------------------------------------------------------


class TestBuildArgv:
    """_build_argv assembles the subprocess command line from the config."""

    def test_minimal_required(self, manager, valid_config):
        """Required flags --hostname/--password/--port present."""
        manager._config = valid_config
        argv = manager._build_argv(valid_config)
        assert WEBCAM_SCRIPT in argv
        assert "--hostname" in argv
        idx = argv.index("--hostname")
        assert argv[idx + 1] == "192.168.1.10"
        assert "--password" in argv
        assert "--port" in argv
        assert "--v4bindaddress" in argv

    def test_width_height_included(self, manager, valid_config):
        """With override on, non-zero width/height produce --width/--height."""
        argv = manager._build_argv(valid_config)
        assert "--width" in argv
        assert "--height" in argv

    def test_zero_width_omitted(self, manager, valid_config):
        """Width/height of zero omit the flags even when override is on."""
        cfg = dict(valid_config, width=0, height=0)
        argv = manager._build_argv(cfg)
        assert "--width" not in argv
        assert "--height" not in argv

    def test_resolution_omitted_when_override_off(self, manager, valid_config):
        """Override off (the default) never forwards --width/--height, even
        with non-zero width/height set in the config."""
        cfg = dict(valid_config, override_resolution=False)
        argv = manager._build_argv(cfg)
        assert "--width" not in argv
        assert "--height" not in argv

    def test_rotate_included(self, manager, valid_config):
        """A rotate value != -1 adds --rotate to the argv."""
        cfg = dict(valid_config, rotate=90)
        argv = manager._build_argv(cfg)
        assert "--rotate" in argv
        idx = argv.index("--rotate")
        assert argv[idx + 1] == "90"

    def test_rotate_minus1_omitted(self, manager, valid_config):
        """rotate=-1 (default) omits the --rotate flag."""
        argv = manager._build_argv(valid_config)
        assert "--rotate" not in argv

    def test_flags_included(self, manager, valid_config):
        """flashred, showfps, loghttp each add their respective flag."""
        cfg = dict(valid_config, flashred=True, showfps=True, loghttp=True)
        argv = manager._build_argv(cfg)
        assert "--flashred" in argv
        assert "--showfps" in argv
        assert "--loghttp" in argv

    def test_flags_absent_by_default(self, manager, valid_config):
        """Boolean flags are absent when set to False."""
        argv = manager._build_argv(valid_config)
        assert "--flashred" not in argv
        assert "--showfps" not in argv
        assert "--loghttp" not in argv

    def test_encodewait_included(self, manager, valid_config):
        """A non-None encodewait adds --encodewait."""
        argv = manager._build_argv(valid_config)
        assert "--encodewait" in argv

    def test_encodewait_none_omitted(self, manager, valid_config):
        """encodewait=None omits the --encodewait flag."""
        cfg = dict(valid_config, encodewait=None)
        argv = manager._build_argv(cfg)
        assert "--encodewait" not in argv


# ---------------------------------------------------------------------------
# _pump_logs
# ---------------------------------------------------------------------------


class TestPumpLogs:
    """_pump_logs routes child stdout to the plugin or HTTP logger."""

    def _process(self, lines):
        """Build a mock process whose stdout yields the given lines."""
        proc = MagicMock()
        proc.stdout = iter(line + "\n" for line in lines)
        return proc

    def test_plugin_log_routing(self, manager):
        """Normal startup messages reach the plugin logger without error."""
        proc = self._process(["startup message"])
        manager._pump_logs(proc)

    def test_http_log_routing(self, manager):
        """Lines matching the HTTP log pattern are sent to the HTTP logger."""
        http_logger = MagicMock()
        manager._http_logger = http_logger
        line = "2024-01-01 12:00:00.123: 127.0.0.1 GET /?stream"
        proc = self._process([line])
        manager._pump_logs(proc)
        http_logger.info.assert_called_once_with(line)

    def test_empty_lines_skipped(self, manager):
        """Blank lines are silently dropped and never reach the HTTP logger."""
        http_logger = MagicMock()
        manager._http_logger = http_logger
        proc = self._process(["", "   ", "real line"])
        manager._pump_logs(proc)
        http_logger.info.assert_not_called()

    def test_exception_swallowed(self, manager):
        """An exception while reading stdout must not propagate."""
        proc = MagicMock()
        proc.stdout = MagicMock()
        proc.stdout.__iter__ = MagicMock(side_effect=RuntimeError("pipe broke"))
        manager._pump_logs(proc)


# ---------------------------------------------------------------------------
# is_running
# ---------------------------------------------------------------------------


class TestIsRunning:
    """is_running reflects the live state of the child process."""

    def test_no_process(self, manager):
        """Returns False when no process has been spawned."""
        assert manager.is_running() is False

    def test_process_alive(self, manager):
        """Returns True while poll() returns None (process still running)."""
        proc = MagicMock()
        proc.poll.return_value = None
        manager._process = proc
        assert manager.is_running() is True

    def test_process_dead(self, manager):
        """Returns False once poll() returns an exit code."""
        proc = MagicMock()
        proc.poll.return_value = 1
        manager._process = proc
        assert manager.is_running() is False


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


class TestStatus:
    """status() returns a snapshot dict for the API/frontend."""

    def test_not_running(self, manager):
        """All fields are None/0/False when the daemon is not running."""
        s = manager.status()
        assert s["running"] is False
        assert s["pid"] is None
        assert s["uptime"] is None
        assert s["restarts"] == 0
        assert s["info"] is None

    def test_running(self, manager):
        """pid, uptime and info are populated while the daemon is alive."""
        proc = MagicMock()
        proc.poll.return_value = None
        proc.pid = 999
        manager._process = proc
        manager._started_at = time.monotonic() - 5
        manager._config = {"port": 18181}

        with patch.object(manager, "fetch_info", return_value={"version": "1"}):
            s = manager.status()

        assert s["running"] is True
        assert s["pid"] == 999
        assert s["uptime"] >= 5
        assert s["info"] == {"version": "1"}

    def test_last_error_included(self, manager):
        """The last error string is always included in the snapshot."""
        manager._last_error = "something went wrong"
        s = manager.status()
        assert s["last_error"] == "something went wrong"


# ---------------------------------------------------------------------------
# fetch_info
# ---------------------------------------------------------------------------


class TestFetchInfo:
    """fetch_info GETs /?info from the daemon and strips the password."""

    def test_no_port_returns_none(self, manager):
        """Returns None when no port is configured."""
        manager._config = {}
        assert manager.fetch_info() is None

    def test_success_redacts_password(self, manager):
        """The password key is removed from the config sub-dict."""
        import io

        manager._config = {"port": 18181}
        payload = json.dumps(
            {"config": {"hostname": "printer", "password": "secret"}}
        ).encode()

        class _FakeResponse:
            def __enter__(self):
                return io.BytesIO(payload)

            def __exit__(self, *_):
                return False

        with patch("urllib.request.urlopen", return_value=_FakeResponse()):
            info = manager.fetch_info()

        assert "password" not in info.get("config", {})
        assert info["config"]["hostname"] == "printer"

    def test_url_error_returns_none(self, manager):
        """A URLError (daemon not yet listening) returns None."""
        import urllib.error

        manager._config = {"port": 18181}
        with patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError("refused"),
        ):
            assert manager.fetch_info() is None

    def test_os_error_returns_none(self, manager):
        """An OSError from urlopen returns None."""
        manager._config = {"port": 18181}
        with patch("urllib.request.urlopen", side_effect=OSError("broken")):
            assert manager.fetch_info() is None

    def test_value_error_returns_none(self, manager):
        """Invalid JSON from the daemon returns None."""
        import io

        manager._config = {"port": 18181}

        class _BadResponse:
            def __enter__(self):
                return io.BytesIO(b"not json {")

            def __exit__(self, *_):
                return False

        with patch("urllib.request.urlopen", return_value=_BadResponse()):
            assert manager.fetch_info() is None


# ---------------------------------------------------------------------------
# stop
# ---------------------------------------------------------------------------


class TestStop:
    """stop() terminates the daemon and supersedes its watchdog."""

    def test_stop_no_process(self, manager):
        """stop() with no process must not raise."""
        manager.stop()
        assert manager._stop_requested is True

    def test_stop_increments_generation(self, manager):
        """stop() advances the generation counter to invalidate watchdog."""
        gen_before = manager._generation
        manager.stop()
        assert manager._generation == gen_before + 1

    def test_stop_terminates_running_process(self, manager):
        """terminate() is called on a running process."""
        proc = MagicMock()
        proc.poll.return_value = None
        proc.wait.return_value = 0
        manager._process = proc
        manager.stop()
        proc.terminate.assert_called_once()

    def test_stop_kills_on_timeout(self, manager):
        """kill() is called when the process ignores SIGTERM."""
        import subprocess

        proc = MagicMock()
        proc.poll.return_value = None
        proc.wait.side_effect = [subprocess.TimeoutExpired("cmd", 5), 0]
        manager._process = proc
        manager.stop()
        proc.kill.assert_called_once()

    def test_stop_notifies(self, manager_with_callback):
        """The 'stopped' state is reported via the callback."""
        m, cb = manager_with_callback
        proc = MagicMock()
        proc.poll.return_value = None
        proc.wait.return_value = 0
        m._process = proc
        m.stop()
        cb.assert_called_once_with("stopped", {})

    def test_stop_already_dead_process(self, manager):
        """terminate() is not called when the process already exited."""
        proc = MagicMock()
        proc.poll.return_value = 1
        manager._process = proc
        manager.stop()
        proc.terminate.assert_not_called()


# ---------------------------------------------------------------------------
# start
# ---------------------------------------------------------------------------


class TestStart:
    """start() validates the config, then spawns the daemon process."""

    def test_start_invalid_config(self, manager, valid_config):
        """An incomplete config causes start() to return (False, error)."""
        cfg = dict(valid_config, hostname="")
        ok, error = manager.start(cfg)
        assert ok is False
        assert error is not None
        assert manager._last_error == error

    def test_start_valid_config(self, manager, valid_config):
        """A valid config spawns the process and returns (True, None)."""
        with patch("os.path.isfile", return_value=True):
            with patch.object(
                WebcamdManager, "port_in_use", return_value=False
            ):
                with patch("subprocess.Popen") as mock_popen:
                    proc = MagicMock()
                    proc.pid = 1234
                    proc.poll.return_value = None
                    proc.stdout = iter([])
                    mock_popen.return_value = proc
                    ok, error = manager.start(valid_config)

        assert ok is True
        assert error is None
        assert manager._process is proc

    def test_start_spawn_failure(self, manager, valid_config):
        """An OSError from Popen is reported as (False, error)."""
        with patch("os.path.isfile", return_value=True):
            with patch.object(
                WebcamdManager, "port_in_use", return_value=False
            ):
                with patch(
                    "subprocess.Popen",
                    side_effect=OSError("no such file"),
                ):
                    ok, error = manager.start(valid_config)
        assert ok is False
        assert "webcamd" in error

    def test_start_clears_stop_requested(self, manager, valid_config):
        """start() resets the stop_requested flag set by a previous stop()."""
        manager._stop_requested = True
        with patch("os.path.isfile", return_value=True):
            with patch.object(
                WebcamdManager, "port_in_use", return_value=False
            ):
                with patch("subprocess.Popen") as mock_popen:
                    proc = MagicMock()
                    proc.pid = 1234
                    proc.poll.return_value = None
                    proc.stdout = iter([])
                    mock_popen.return_value = proc
                    manager.start(valid_config)
        assert manager._stop_requested is False


# ---------------------------------------------------------------------------
# restart
# ---------------------------------------------------------------------------


class TestRestart:
    """restart() stops the daemon then starts it with given/stored config."""

    def test_restart_uses_new_config(self, manager, valid_config):
        """A new config passed to restart() is forwarded to start()."""
        new_cfg = dict(valid_config, hostname="192.168.1.99")
        with patch.object(manager, "stop"):
            with patch.object(
                manager, "start", return_value=(True, None)
            ) as mock_start:
                manager.restart(new_cfg)
        mock_start.assert_called_once_with(new_cfg)

    def test_restart_uses_stored_config_when_none(self, manager, valid_config):
        """Passing None reuses the last active config."""
        manager._config = dict(valid_config)
        stored = manager._config
        with patch.object(manager, "stop"):
            with patch.object(
                manager, "start", return_value=(True, None)
            ) as mock_start:
                manager.restart(None)
        mock_start.assert_called_once_with(stored)

    def test_restart_clears_timestamps(self, manager, valid_config):
        """restart() resets the restart-rate timestamp list."""
        manager._restart_timestamps = [1.0, 2.0, 3.0]
        with patch.object(manager, "stop"):
            with patch.object(manager, "start", return_value=(True, None)):
                manager.restart(valid_config)
        assert manager._restart_timestamps == []


# ---------------------------------------------------------------------------
# _spawn
# ---------------------------------------------------------------------------


class TestSpawn:
    """_spawn launches the subprocess and wires up the watchdog thread."""

    def test_spawn_increments_generation(self, manager, valid_config):
        """Each spawn advances the generation counter by one."""
        manager._config = valid_config
        gen_before = manager._generation
        with patch("subprocess.Popen") as mock_popen:
            proc = MagicMock()
            proc.pid = 42
            proc.stdout = iter([])
            mock_popen.return_value = proc
            manager._spawn()
        assert manager._generation == gen_before + 1

    def test_spawn_notifies_started(self, manager_with_callback, valid_config):
        """_spawn fires the 'started' callback with the process PID."""
        m, cb = manager_with_callback
        m._config = valid_config
        with patch("subprocess.Popen") as mock_popen:
            proc = MagicMock()
            proc.pid = 42
            proc.poll.return_value = None
            proc.stdout = iter([])
            mock_popen.return_value = proc
            m._spawn()
        cb.assert_called_once_with("started", {"pid": 42})

    def test_spawn_oserror(self, manager, valid_config):
        """An OSError from Popen is returned as (False, error_string)."""
        manager._config = valid_config
        with patch("subprocess.Popen", side_effect=OSError("exec failed")):
            ok, error = manager._spawn()
        assert ok is False
        assert "webcamd" in error


# ---------------------------------------------------------------------------
# _watchdog integration
# ---------------------------------------------------------------------------


class TestWatchdog:
    """Exercise watchdog logic synchronously by calling _watchdog directly."""

    def test_exits_on_generation_mismatch(self, manager):
        """A stale watchdog (wrong generation) exits immediately."""
        proc = MagicMock()
        proc.poll.return_value = None
        manager._generation = 5
        manager._started_at = time.monotonic()
        manager._watchdog(proc, 3)

    def test_exits_on_stop_requested(self, manager):
        """The watchdog exits when stop_requested is set after a crash."""
        returns = [None, 1]

        def _poll():
            return returns.pop(0) if returns else 1

        proc = MagicMock()
        proc.poll = _poll
        manager._generation = 1
        manager._stop_requested = True
        manager._started_at = time.monotonic()

        with patch("time.sleep"):
            manager._watchdog(proc, 1)

    def test_no_autorestart_exits_on_crash(self, manager, valid_config):
        """With autorestart=False the watchdog exits after the first crash."""
        proc = MagicMock()
        proc.poll.return_value = 1
        manager._generation = 1
        manager._config = dict(valid_config, autorestart=False)
        manager._started_at = time.monotonic()

        manager._watchdog(proc, 1)
        assert manager._restart_count == 0

    def test_notifies_crashed(self, manager_with_callback, valid_config):
        """An unexpected exit fires the 'crashed' callback."""
        m, cb = manager_with_callback
        proc = MagicMock()
        proc.poll.return_value = 2
        m._generation = 1
        m._config = dict(valid_config, autorestart=False)
        m._started_at = time.monotonic()

        m._watchdog(proc, 1)
        cb.assert_any_call("crashed", {"returncode": 2})

    def test_gave_up_after_max_restarts(
        self, manager_with_callback, valid_config
    ):
        """Exceeding max_restarts fires 'gave_up' and stops restarting."""
        m, cb = manager_with_callback
        proc = MagicMock()
        proc.poll.return_value = 1
        m._generation = 1
        m._config = dict(
            valid_config,
            autorestart=True,
            max_restarts=2,
            restart_window=300,
        )
        m._started_at = time.monotonic()
        now = time.monotonic()
        m._restart_timestamps = [now, now, now]

        with patch("time.sleep"):
            m._watchdog(proc, 1)

        states = [c.args[0] for c in cb.call_args_list]
        assert "gave_up" in states

    def test_restarts_process(self, manager, valid_config):
        """After a crash the watchdog calls _spawn and bumps restart_count."""
        proc = MagicMock()
        proc.poll.return_value = 1
        manager._generation = 1
        manager._config = dict(valid_config, autorestart=True, max_restarts=5)
        manager._started_at = time.monotonic()

        with patch("time.sleep"):
            with patch.object(
                manager, "_spawn", return_value=(True, None)
            ) as mock_spawn:
                manager._watchdog(proc, 1)

        mock_spawn.assert_called_once()
        assert manager._restart_count == 1

    def test_healthy_run_resets_backoff(self, manager, valid_config):
        """Process healthy for >60 s should reset the backoff counter."""
        returns = [None, None, 1]

        def _poll():
            return returns.pop(0) if returns else 1

        proc = MagicMock()
        proc.poll = _poll
        manager._generation = 1
        manager._config = dict(valid_config, autorestart=False)
        manager._started_at = time.monotonic() - 65

        sleep_calls = []

        def _sleep(s):
            sleep_calls.append(s)
            if len(sleep_calls) >= 2:
                manager._generation = 99

        with patch("time.sleep", side_effect=_sleep):
            manager._watchdog(proc, 1)

    def test_spawn_failure_sets_last_error(self, manager, valid_config):
        """A failed _spawn during watchdog restart records the error."""
        proc = MagicMock()
        proc.poll.return_value = 1
        manager._generation = 1
        manager._config = dict(valid_config, autorestart=True, max_restarts=5)
        manager._started_at = time.monotonic()

        with patch("time.sleep"):
            with patch.object(
                manager,
                "_spawn",
                return_value=(False, "launch failed"),
            ):
                manager._watchdog(proc, 1)

        assert manager._last_error == "launch failed"
