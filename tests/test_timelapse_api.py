"""Tests for the timelapse FTPS API commands on BambucamPlugin."""

# pylint: disable=protected-access,redefined-outer-name,too-few-public-methods
# pylint: disable=missing-function-docstring,missing-class-docstring
# pylint: disable=unused-argument,import-outside-toplevel

import datetime
import os
from unittest.mock import MagicMock, patch

import flask
import pytest

from octoprint_bambucam import ftp as ftp_mod


@pytest.fixture()
def app():
    """A minimal Flask app so jsonify() has a request/app context."""
    return flask.Flask(__name__)


@pytest.fixture(autouse=True)
def grant_permissions():
    """Grant all permissions for the duration of each test."""
    with patch("octoprint_bambucam.Permissions") as perms:
        perms.SETTINGS.can.return_value = True
        perms.ADMIN.can.return_value = True
        perms.CONTROL.can.return_value = True
        yield perms


@pytest.fixture(autouse=True)
def no_event_manager():
    """Stub OctoPrint's eventManager so MovieDone firing is a no-op in tests."""
    with patch("octoprint_bambucam.timelapse_ops.eventManager") as em:
        yield em


def _json(plugin, command, data, app):
    if plugin._manager is None:
        plugin._manager = MagicMock()
    with app.test_request_context():
        resp = plugin.on_api_command(command, data)
        return resp.get_json()


# ---------------------------------------------------------------------------
# get_api_commands
# ---------------------------------------------------------------------------


class TestApiCommands:
    def test_new_commands_registered(self, plugin):
        cmds = plugin.get_api_commands()
        for name in (
            "list_timelapses",
            "copy_timelapses",
            "move_timelapses",
            "delete_timelapses",
            "ffmpeg_status",
        ):
            assert name in cmds


class TestSetLed:
    def test_led_on_publishes(self, plugin, app):
        calls = []

        class FakeMqtt:
            def set_chamber_light(self, on):
                calls.append(on)

        plugin._make_mqtt = FakeMqtt
        out = _json(plugin, "set_led", {"on": True}, app)
        assert out == {"ok": True}
        assert calls == [True]

    def test_led_off_publishes(self, plugin, app):
        calls = []

        class FakeMqtt:
            def set_chamber_light(self, on):
                calls.append(on)

        plugin._make_mqtt = FakeMqtt
        out = _json(plugin, "set_led", {"on": False}, app)
        assert out == {"ok": True}
        assert calls == [False]

    def test_led_reports_reason_on_mqtt_error(self, plugin, app):
        from octoprint_bambucam.mqtt import MqttError

        class FakeMqtt:
            def set_chamber_light(self, on):
                raise MqttError("auth_failed")

        plugin._make_mqtt = FakeMqtt
        out = _json(plugin, "set_led", {"on": True}, app)
        assert out["ok"] is False
        assert out["reason"] == "auth_failed"

    def test_led_reports_error_on_unexpected(self, plugin, app):
        class FakeMqtt:
            def set_chamber_light(self, on):
                raise RuntimeError("boom")

        plugin._make_mqtt = FakeMqtt
        out = _json(plugin, "set_led", {"on": True}, app)
        assert out["ok"] is False
        assert out["reason"] == "error"

    def test_led_requires_control_permission(self, plugin, app):
        with patch("octoprint_bambucam.Permissions") as perms:
            perms.CONTROL.can.return_value = False
            with app.test_request_context():
                with pytest.raises(Exception):  # flask.abort(403)
                    plugin.on_api_command("set_led", {"on": True})

    def test_led_rejects_concurrent_command(self, plugin, app):
        # a command already in flight → second one is refused, no MQTT opened
        plugin._led_busy = True

        class FakeMqtt:
            def set_chamber_light(self, on):
                raise AssertionError("must not connect while busy")

        plugin._make_mqtt = FakeMqtt
        out = _json(plugin, "set_led", {"on": True}, app)
        assert out == {"ok": False, "reason": "busy"}

    def test_led_clears_busy_after_command(self, plugin, app):
        class FakeMqtt:
            def set_chamber_light(self, on):
                pass

        plugin._make_mqtt = FakeMqtt
        _json(plugin, "set_led", {"on": True}, app)
        assert plugin._led_busy is False

    def test_set_led_uses_monitor_when_open(self, plugin, app):
        calls = []

        class FakeMonitor:
            def set_chamber_light(self, on):
                calls.append(on)

        plugin._led_monitor = FakeMonitor()
        # one-shot path must NOT be used when a monitor is open
        plugin._make_mqtt = lambda: (_ for _ in ()).throw(
            AssertionError("should reuse the monitor connection")
        )
        out = _json(plugin, "set_led", {"on": True}, app)
        assert out == {"ok": True}
        assert calls == [True]

    def test_set_led_prefers_connector(self, plugin, app):
        sent = []
        with patch(
            "octoprint_bambucam.connector_led.set_chamber_light"
        ) as cset:
            cset.side_effect = lambda _printer, on, _lg=None: (
                sent.append(on) or True
            )
            plugin._led_monitor = None
            plugin._make_mqtt = lambda: (_ for _ in ()).throw(
                AssertionError("connector path should win")
            )
            out = _json(plugin, "set_led", {"on": True}, app)
        assert out == {"ok": True}
        assert sent == [True]


class TestLedMonitor:
    def test_start_uses_connector_when_available(self, plugin, app):
        with (
            patch(
                "octoprint_bambucam.connector_led.available", return_value=True
            ),
            patch(
                "octoprint_bambucam.connector_led.current_state",
                return_value=True,
            ),
        ):
            plugin._make_mqtt = lambda: (_ for _ in ()).throw(
                AssertionError("must not open own monitor")
            )
            out = _json(plugin, "led_monitor_start", {}, app)
        assert out == {"ok": True, "led_on": True}
        assert plugin._led_monitor is None

    def test_start_returns_no_serial_without_connector(self, plugin, app):
        plugin._effective_serial = lambda: ""
        with patch(
            "octoprint_bambucam.connector_led.available", return_value=False
        ):
            out = _json(plugin, "led_monitor_start", {}, app)
        assert out == {"ok": False, "reason": "no_serial"}

    def test_start_opens_monitor_and_returns_state(self, plugin, app):
        plugin._effective_serial = lambda: "SER1"
        plugin._effective_credentials = lambda: ("10.0.0.5", "code")

        class FakeMonitor:
            def __init__(self, *a, **k):
                pass

            def start(self):
                pass

        with (
            patch("octoprint_bambucam.BambuMqttMonitor", FakeMonitor),
            patch(
                "octoprint_bambucam.connector_led.available", return_value=False
            ),
        ):
            out = _json(plugin, "led_monitor_start", {}, app)
        assert out["ok"] is True
        assert plugin._led_monitor is not None

    def test_start_is_idempotent(self, plugin, app):
        sentinel = object()
        plugin._led_monitor = sentinel
        plugin._led_state = True
        out = _json(plugin, "led_monitor_start", {}, app)
        assert out == {"ok": True, "led_on": True}
        assert plugin._led_monitor is sentinel  # not replaced

    def test_start_reports_mqtt_failure(self, plugin, app):
        from octoprint_bambucam.mqtt import MqttError

        plugin._effective_serial = lambda: "SER1"
        plugin._effective_credentials = lambda: ("10.0.0.5", "code")

        class FailMonitor:
            def __init__(self, *a, **k):
                pass

            def start(self):
                raise MqttError("unreachable")

        with (
            patch("octoprint_bambucam.BambuMqttMonitor", FailMonitor),
            patch(
                "octoprint_bambucam.connector_led.available", return_value=False
            ),
        ):
            out = _json(plugin, "led_monitor_start", {}, app)
        assert out == {"ok": False, "reason": "unreachable"}
        assert plugin._led_monitor is None

    def test_stop_tears_down_monitor(self, plugin, app):
        stopped = []

        class FakeMonitor:
            def stop(self):
                stopped.append(True)

        plugin._led_monitor = FakeMonitor()
        out = _json(plugin, "led_monitor_stop", {}, app)
        assert out == {"ok": True}
        assert stopped == [True]
        assert plugin._led_monitor is None

    def test_state_change_pushes_message(self, plugin):
        plugin._on_led_state_change(True)
        assert plugin._led_state is True
        msg = plugin._plugin_manager.send_plugin_message.call_args.args[1]
        assert msg == {"type": "led_state", "on": True}

    def test_status_get_carries_led_on(self, plugin, app):
        plugin._manager = MagicMock()
        plugin._manager.status.return_value = {"running": True}
        plugin._stream_url = lambda: ""
        plugin._effective_serial = lambda: "SER1"
        plugin._led_state = False
        with patch(
            "octoprint_bambucam.connector_led.current_state", return_value=None
        ):
            with app.test_request_context("/"):
                resp = plugin.on_api_get(flask.request)
        assert resp.get_json()["led_on"] is False

    def test_status_get_prefers_connector_state(self, plugin, app):
        plugin._manager = MagicMock()
        plugin._manager.status.return_value = {"running": True}
        plugin._stream_url = lambda: ""
        plugin._effective_serial = lambda: "SER1"
        plugin._led_state = False  # stale monitor value
        with patch(
            "octoprint_bambucam.connector_led.current_state", return_value=True
        ):
            with app.test_request_context("/"):
                resp = plugin.on_api_get(flask.request)
        assert resp.get_json()["led_on"] is True  # connector wins


class TestFfmpegStatus:
    def test_reports_available(self, plugin, app):
        plugin._make_transcoder = lambda: FakeTranscoder(avail=True)
        out = _json(plugin, "ffmpeg_status", {}, app)
        assert out["ok"] is True
        assert out["ffmpeg"]["executable"] is True
        assert out["ffmpeg"]["path"] == "/usr/bin/ffmpeg"

    def test_reports_missing(self, plugin, app):
        plugin._make_transcoder = lambda: FakeTranscoder(avail=False)
        out = _json(plugin, "ffmpeg_status", {}, app)
        assert out["ok"] is True
        assert out["ffmpeg"]["executable"] is False
        assert out["ffmpeg"]["path"] == ""


# ---------------------------------------------------------------------------
# permission gating
# ---------------------------------------------------------------------------


class TestPermissions:
    def test_admin_required_for_copy(self, plugin, app):
        with patch("octoprint_bambucam.Permissions") as perms:
            perms.SETTINGS.can.return_value = True
            perms.ADMIN.can.return_value = False
            with app.test_request_context():
                with pytest.raises(Exception):  # flask.abort(403)
                    plugin.on_api_command(
                        "copy_timelapses", {"names": ["a.mp4"]}
                    )

    def test_settings_required_for_list(self, plugin, app):
        with patch("octoprint_bambucam.Permissions") as perms:
            perms.SETTINGS.can.return_value = False
            with app.test_request_context():
                with pytest.raises(Exception):
                    plugin.on_api_command("list_timelapses", {})


# ---------------------------------------------------------------------------
# filename build
# ---------------------------------------------------------------------------


class TestFilenameBuild:
    def test_suffix_inserted_before_ext(self, plugin, tmp_path):
        plugin._settings.get = lambda k: (
            "_bambu" if k == ["download_suffix"] else ""
        )
        dest = plugin._build_dest_path(str(tmp_path), "video1.mp4", None)
        assert os.path.basename(dest) == "video1_bambu.mp4"

    def test_no_suffix(self, plugin, tmp_path):
        plugin._settings.get = lambda k: ""
        dest = plugin._build_dest_path(str(tmp_path), "video1.mp4", None)
        assert os.path.basename(dest) == "video1.mp4"

    def test_invalid_suffix_falls_back_to_none(self, plugin, tmp_path):
        plugin._settings.get = lambda k: (
            "../bad" if k == ["download_suffix"] else ""
        )
        dest = plugin._build_dest_path(str(tmp_path), "video1.mp4", None)
        # ../ stripped → suffix becomes "bad"? sanitize keeps "bad"; ensure no /
        assert "/" not in os.path.basename(dest)
        assert os.path.basename(dest).endswith(".mp4")

    def test_collision_dedup(self, plugin, tmp_path):
        plugin._settings.get = lambda k: ""
        (tmp_path / "video1.mp4").write_text("x")
        dest = plugin._build_dest_path(str(tmp_path), "video1.mp4", None)
        assert os.path.basename(dest) == "video1-1.mp4"

    def test_suffix_length_capped(self, plugin, tmp_path):
        plugin._settings.get = lambda k: (
            "x" * 100 if k == ["download_suffix"] else ""
        )
        suffix = plugin._sanitized_suffix()
        assert len(suffix) <= 32

    def test_collision_cap_returns_conflict(self, plugin, tmp_path):
        plugin._settings.get = lambda k: ""
        result = plugin._collision_safe(str(tmp_path), "v", ".mp4")
        assert result == "v.mp4"
        # force the cap
        with patch("octoprint_bambucam.timelapse_ops.MAX_COLLISION", 2):
            (tmp_path / "v.mp4").write_text("x")
            (tmp_path / "v-1.mp4").write_text("x")
            (tmp_path / "v-2.mp4").write_text("x")
            assert plugin._collision_safe(str(tmp_path), "v", ".mp4") is None

    def test_build_dest_returns_name_conflict_at_cap(self, plugin, tmp_path):
        plugin._settings.get = lambda k: ""
        with patch("octoprint_bambucam.timelapse_ops.MAX_COLLISION", 1):
            (tmp_path / "video1.mp4").write_text("x")
            (tmp_path / "video1-1.mp4").write_text("x")
            result = plugin._build_dest_path(str(tmp_path), "video1.mp4", None)
        assert result == "@reason:name_conflict"


# ---------------------------------------------------------------------------
# §5.7 file safety
# ---------------------------------------------------------------------------


class TestFileSafety:
    def test_no_space_refuses(self, plugin, tmp_path):
        plugin._settings.get = lambda k: ""
        with patch("octoprint_bambucam.timelapse_ops.shutil.disk_usage") as du:
            du.return_value = MagicMock(free=10)
            dest = plugin._build_dest_path(
                str(tmp_path), "video1.mp4", 1_000_000
            )
        assert dest is None  # no_space

    def test_containment_rejects_escape(self, plugin):
        assert not plugin._is_contained("/etc/passwd", "/tmp/base")
        assert plugin._is_contained("/tmp/base/x.mp4", "/tmp/base")

    def test_empty_name_uses_fallback(self, plugin, tmp_path):
        plugin._settings.get = lambda k: ""
        dest = plugin._build_dest_path(str(tmp_path), ".mp4", None)
        assert os.path.basename(dest) == "bambu-timelapse.mp4"


# ---------------------------------------------------------------------------
# busy reject
# ---------------------------------------------------------------------------


class TestBusyReject:
    def test_second_op_rejected(self, plugin, app):
        plugin._ftp_busy = True
        out = _json(plugin, "copy_timelapses", {"names": ["a.mp4"]}, app)
        assert out["ok"] is False
        assert out["reason"] == "busy"

    def test_busy_cleared_after_worker(self, plugin):
        plugin._ftp_busy = True

        def fake_make_ftp():
            raise RuntimeError("boom")

        plugin._make_ftp = fake_make_ftp
        plugin._run_batch("copy", ["a.mp4"])
        assert plugin._ftp_busy is False


# ---------------------------------------------------------------------------
# print-active guard (§5.9)
# ---------------------------------------------------------------------------


class TestPrintGuard:
    def test_move_blocked_while_printing(self, plugin, app):
        plugin._printer.is_printing.return_value = True
        out = _json(plugin, "move_timelapses", {"names": ["a.mp4"]}, app)
        assert out == {"ok": False, "reason": "printing"}

    def test_delete_blocked_while_paused(self, plugin, app):
        plugin._printer.is_paused.return_value = True
        out = _json(plugin, "delete_timelapses", {"names": ["a.mp4"]}, app)
        assert out["reason"] == "printing"

    def test_copy_allowed_while_printing(self, plugin, app):
        plugin._printer.is_printing.return_value = True
        with patch.object(plugin, "_run_batch"):
            out = _json(plugin, "copy_timelapses", {"names": ["a.mp4"]}, app)
        assert out["ok"] is True


# ---------------------------------------------------------------------------
# batch behaviour (mixed names, move=copy+delete) over a fake FTP service
# ---------------------------------------------------------------------------


class FakeService:
    """Fake BambuTimelapseFtp for batch tests."""

    def __init__(self, listing, sizes, *, written=None, fail=()):
        self._listing = listing
        self._sizes = sizes
        self._written = written or {}
        self._fail = fail
        self.deleted = []

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def list_timelapses(self):
        return [{"name": n} for n in self._listing]

    def remote_size(self, name):
        return self._sizes.get(name)

    def download(self, name, dest, *, progress_cb=None):
        if name in self._fail:
            raise OSError("short")
        if progress_cb:
            progress_cb(1, 1)
        with open(dest, "wb") as fh:
            fh.write(b"x" * self._written.get(name, self._sizes.get(name, 1)))
        return self._written.get(name, self._sizes.get(name, 1))

    def delete(self, name):
        self.deleted.append(name)


class TestBatch:
    def test_mixed_batch_continues(self, plugin, tmp_path):
        plugin._settings.get = lambda k: ""
        plugin._settings.global_get_basefolder = lambda _x: str(tmp_path)
        svc = FakeService(
            listing=["good.mp4", "bad.mp4"],
            sizes={"good.mp4": 5, "bad.mp4": 5},
            fail=("bad.mp4",),
        )
        plugin._make_ftp = lambda: svc
        pm = plugin._plugin_manager
        # unknown.mp4 not in listing → skipped; good copies; bad fails
        plugin._run_batch("copy", ["good.mp4", "unknown.mp4", "bad.mp4"])
        states = [c.args[1] for c in pm.send_plugin_message.call_args_list]
        # final message is batch_done
        assert states[-1]["state"] == "batch_done"
        summary = states[-1]["summary"]
        assert summary["copied"] == 1
        assert summary["skipped"] == 2
        assert os.path.exists(tmp_path / "good.mp4")

    def test_copy_stamps_real_mtime(self, plugin, tmp_path):
        """A copied file's mtime is set to ~now (the real copy time), not the
        wrong camera date, so it sorts/shows correctly in the native tab."""
        import time as _time

        plugin._settings.get = lambda k: ""
        plugin._settings.global_get_basefolder = lambda _x: str(tmp_path)
        svc = FakeService(listing=["a.mp4"], sizes={"a.mp4": 5})
        plugin._make_ftp = lambda: svc
        before = _time.time()
        plugin._run_batch("copy", ["a.mp4"])
        mtime = os.path.getmtime(tmp_path / "a.mp4")
        assert abs(mtime - before) < 60

    def test_move_deletes_only_on_verified_copy(self, plugin, tmp_path):
        plugin._settings.get = lambda k: ""
        plugin._settings.global_get_basefolder = lambda _x: str(tmp_path)
        svc = FakeService(
            listing=["a.mp4", "b.mp4"],
            sizes={"a.mp4": 5, "b.mp4": 5},
            written={"a.mp4": 5, "b.mp4": 3},  # b is short → no delete
        )
        plugin._make_ftp = lambda: svc
        plugin._run_batch("move", ["a.mp4", "b.mp4"])
        assert svc.deleted == ["a.mp4"]

    def test_delete_op_issues_dele(self, plugin, tmp_path):
        plugin._settings.global_get_basefolder = lambda _x: str(tmp_path)
        svc = FakeService(listing=["a.mp4"], sizes={"a.mp4": 5})
        plugin._make_ftp = lambda: svc
        plugin._run_batch("delete", ["a.mp4"])
        assert svc.deleted == ["a.mp4"]

    def test_no_space_in_batch(self, plugin, tmp_path):
        plugin._settings.get = lambda k: ""
        plugin._settings.global_get_basefolder = lambda _x: str(tmp_path)
        svc = FakeService(listing=["a.mp4"], sizes={"a.mp4": 1_000_000})
        plugin._make_ftp = lambda: svc
        pm = plugin._plugin_manager
        with patch("octoprint_bambucam.timelapse_ops.shutil.disk_usage") as du:
            du.return_value = MagicMock(free=10)
            plugin._run_batch("copy", ["a.mp4"])
        msgs = [c.args[1] for c in pm.send_plugin_message.call_args_list]
        reasons = [m.get("reason") for m in msgs]
        assert "no_space" in reasons

    def test_connect_failure_emits_error(self, plugin, tmp_path):
        plugin._settings.global_get_basefolder = lambda _x: str(tmp_path)

        def boom():
            raise ftp_mod.FtpError("unreachable")

        plugin._make_ftp = boom
        pm = plugin._plugin_manager
        plugin._run_batch("copy", ["a.mp4"])
        msgs = [c.args[1] for c in pm.send_plugin_message.call_args_list]
        assert any(m.get("reason") == "unreachable" for m in msgs)
        assert plugin._ftp_busy is False

    def test_bad_names_rejected_synchronously(self, plugin, app):
        out = _json(plugin, "copy_timelapses", {"names": []}, app)
        assert out["reason"] == "bad_name"


# ---------------------------------------------------------------------------
# list_timelapses with copied marker (§5.8)
# ---------------------------------------------------------------------------


class TestListCopiedMarker:
    def test_copied_true_when_local_exists(self, plugin, app, tmp_path):
        plugin._settings.get = lambda k: ""
        plugin._settings.global_get_basefolder = lambda _x: str(tmp_path)
        (tmp_path / "a.mp4").write_text("x")

        class ListSvc(FakeService):
            def list_timelapses(self):
                return [
                    {"name": "a.mp4", "size": 1, "date": None},
                    {"name": "b.mp4", "size": 1, "date": None},
                ]

        plugin._make_ftp = lambda: ListSvc([], {})
        out = _json(plugin, "list_timelapses", {}, app)
        assert out["ok"] is True
        by = {f["name"]: f["copied"] for f in out["files"]}
        assert by["a.mp4"] is True
        assert by["b.mp4"] is False

    def test_copied_true_for_transcoded_avi(self, plugin, app, tmp_path):
        """An .avi counts as copied when only its transcoded .mp4 exists."""
        plugin._settings.get = lambda k: ""
        plugin._settings.global_get_basefolder = lambda _x: str(tmp_path)
        (tmp_path / "clip.mp4").write_text("x")  # .avi was transcoded + removed

        class ListSvc(FakeService):
            def list_timelapses(self):
                return [{"name": "clip.avi", "size": 1, "date": None}]

        plugin._make_ftp = lambda: ListSvc([], {})
        out = _json(plugin, "list_timelapses", {}, app)
        assert out["files"][0]["copied"] is True
        # the local .mp4 name is surfaced so the "→ name" hint survives a
        # restart (the live convert event is gone after a reload)
        assert out["files"][0]["renamed"] == "clip.mp4"

    def test_no_renamed_when_local_name_matches(self, plugin, app, tmp_path):
        """A plain .mp4 copied 1:1 carries no 'renamed' (names are equal)."""
        plugin._settings.get = lambda k: ""
        plugin._settings.global_get_basefolder = lambda _x: str(tmp_path)
        (tmp_path / "a.mp4").write_text("x")

        class ListSvc(FakeService):
            def list_timelapses(self):
                return [{"name": "a.mp4", "size": 1, "date": None}]

        plugin._make_ftp = lambda: ListSvc([], {})
        out = _json(plugin, "list_timelapses", {}, app)
        assert out["files"][0]["copied"] is True
        assert "renamed" not in out["files"][0]


# ---------------------------------------------------------------------------
# date handling in the listing (copied → real mtime; uncopied → raw)
# ---------------------------------------------------------------------------


class TestDateHandling:
    def test_copied_file_uses_real_mtime(self, plugin, app, tmp_path):
        """A copied file shows its real local mtime (set at copy time), not the
        wrong camera date the printer stamped on the SD."""
        import os as _os
        import time as _time

        plugin._settings.get = lambda k: ""
        plugin._settings.global_get_basefolder = lambda _x: str(tmp_path)
        local = tmp_path / "video_2026-05-18_03-02-39.avi"
        local.write_text("x")
        now = _time.time()
        _os.utime(local, (now, now))

        class ListSvc(FakeService):
            def list_timelapses(self):
                return [{"name": "video_2026-05-18_03-02-39.avi", "size": 1}]

        plugin._make_ftp = lambda: ListSvc([], {})
        out = _json(plugin, "list_timelapses", {}, app)
        f = out["files"][0]
        assert f["copied"] is True
        assert f["date_corrected"] is True
        expect = datetime.datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M")
        assert f["date"] == expect

    def test_uncopied_unknown_date_is_flagged_unreliable(
        self, plugin, app, tmp_path
    ):
        """An uncopied file with no recorded print date is not date_corrected
        and is flagged unreliable (the bogus camera-clock date must not be
        presented as real)."""
        plugin._settings.get = lambda k: ""
        plugin._settings.global_get_basefolder = lambda _x: str(tmp_path)

        class ListSvc(FakeService):
            def list_timelapses(self):
                return [
                    {
                        "name": "video_2026-05-18_17-18-25.avi",
                        "size": 1,
                        "date": None,
                    }
                ]

        plugin._make_ftp = lambda: ListSvc([], {})
        out = _json(plugin, "list_timelapses", {}, app)
        f = out["files"][0]
        assert f["copied"] is False
        assert "date_corrected" not in f
        assert f["date_unreliable"] is True

    def test_uncopied_file_uses_recorded_print_date(
        self, plugin, app, tmp_path
    ):
        """An uncopied file whose name has a recorded PrintDone time shows that
        real date and is date_corrected (not unreliable)."""
        recorded = {"video_2026-05-18_17-18-25.avi": "2026-06-22 14:42"}
        plugin._settings.get = lambda k: (
            recorded if k == ["print_dates"] else ""
        )
        plugin._settings.global_get_basefolder = lambda _x: str(tmp_path)

        class ListSvc(FakeService):
            def list_timelapses(self):
                return [
                    {
                        "name": "video_2026-05-18_17-18-25.avi",
                        "size": 1,
                        "date": None,
                    }
                ]

        plugin._make_ftp = lambda: ListSvc([], {})
        out = _json(plugin, "list_timelapses", {}, app)
        f = out["files"][0]
        assert f["copied"] is False
        assert f["date_corrected"] is True
        assert f["date"] == "2026-06-22 14:42"
        assert "date_unreliable" not in f


# ---------------------------------------------------------------------------
# transcode integration (copy/move of .avi)
# ---------------------------------------------------------------------------


class FakeTranscoder:
    def __init__(self, *, ok=True, avail=True):
        self._ok = ok
        self._avail = avail
        self.called = False

    def available(self):
        return self._avail

    def status(self):
        return {
            "path": "/usr/bin/ffmpeg" if self._avail else "",
            "configured": self._avail,
            "executable": self._avail,
        }

    def transcode(self, avi, mp4, *, progress_cb=None):
        self.called = True
        if progress_cb:
            progress_cb(50)
            progress_cb(100)
        if not self._ok:
            from octoprint_bambucam.transcode import TranscodeError

            raise TranscodeError("ffmpeg_failed")
        os.replace(avi, mp4)  # pretend re-encode by moving bytes

    def create_thumbnail(self, mp4, thumb):
        return False


class TestTranscodeIntegration:
    def _setup(self, plugin, tmp_path, transcode_on=True):
        plugin._settings.get = lambda k: ""
        plugin._settings.get_boolean = lambda k: transcode_on
        plugin._settings.global_get_basefolder = lambda _x: str(tmp_path)
        svc = FakeService(listing=["clip.avi"], sizes={"clip.avi": 5})
        plugin._make_ftp = lambda: svc
        return svc

    def test_avi_transcoded_and_removed(self, plugin, tmp_path):
        self._setup(plugin, tmp_path)
        tc = FakeTranscoder(ok=True)
        plugin._make_transcoder = lambda: tc
        pm = plugin._plugin_manager
        plugin._run_batch("copy", ["clip.avi"])
        assert tc.called
        assert os.path.exists(tmp_path / "clip.mp4")
        assert not os.path.exists(tmp_path / "clip.avi")
        # a "converting" state is emitted before the final "done"
        states = [
            c.args[1]["state"]
            for c in pm.send_plugin_message.call_args_list
            if c.args[1].get("name") == "clip.avi"
        ]
        assert "converting" in states
        assert states.index("converting") < states.index("done")

    def test_no_converting_state_when_transcode_off(self, plugin, tmp_path):
        self._setup(plugin, tmp_path, transcode_on=False)
        plugin._make_transcoder = lambda: FakeTranscoder(ok=True)
        pm = plugin._plugin_manager
        plugin._run_batch("copy", ["clip.avi"])
        states = [
            c.args[1].get("state")
            for c in pm.send_plugin_message.call_args_list
        ]
        assert "converting" not in states

    def test_transcode_failure_keeps_avi_warns(self, plugin, tmp_path):
        self._setup(plugin, tmp_path)
        tc = FakeTranscoder(ok=False)
        plugin._make_transcoder = lambda: tc
        pm = plugin._plugin_manager
        plugin._run_batch("copy", ["clip.avi"])
        assert os.path.exists(tmp_path / "clip.avi")  # kept
        msgs = [c.args[1] for c in pm.send_plugin_message.call_args_list]
        done = [m for m in msgs if m.get("state") == "done"]
        assert done and done[0]["reason"] == "transcode_failed"
        # the copy itself still counts as success
        assert msgs[-1]["summary"]["copied"] == 1

    def test_transcode_disabled_keeps_avi(self, plugin, tmp_path):
        self._setup(plugin, tmp_path, transcode_on=False)
        tc = FakeTranscoder(ok=True)
        plugin._make_transcoder = lambda: tc
        plugin._run_batch("copy", ["clip.avi"])
        assert not tc.called
        assert os.path.exists(tmp_path / "clip.avi")

    def test_transcode_skipped_while_printing(self, plugin, tmp_path):
        self._setup(plugin, tmp_path)
        plugin._printer.is_printing.return_value = True
        tc = FakeTranscoder(ok=True)
        plugin._make_transcoder = lambda: tc
        pm = plugin._plugin_manager
        # copy is allowed while printing; transcode must be skipped
        plugin._run_batch("copy", ["clip.avi"])
        assert not tc.called
        assert os.path.exists(tmp_path / "clip.avi")
        msgs = [c.args[1] for c in pm.send_plugin_message.call_args_list]
        done = [m for m in msgs if m.get("state") == "done"]
        assert done[0]["reason"] == "printing"

    def test_no_ffmpeg_keeps_avi(self, plugin, tmp_path):
        self._setup(plugin, tmp_path)
        tc = FakeTranscoder(avail=False)
        plugin._make_transcoder = lambda: tc
        pm = plugin._plugin_manager
        plugin._run_batch("copy", ["clip.avi"])
        assert os.path.exists(tmp_path / "clip.avi")
        msgs = [c.args[1] for c in pm.send_plugin_message.call_args_list]
        done = [m for m in msgs if m.get("state") == "done"]
        assert done[0]["reason"] == "no_ffmpeg"


# ---------------------------------------------------------------------------
# thumbnail proxy (on_api_get ?thumb=)
# ---------------------------------------------------------------------------


class ThumbSvc:
    def __init__(self, data):
        self._data = data

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def fetch_thumbnail(self, name):
        return self._data


class TestLedAvailability:
    def test_status_flags_led_available_with_serial(self, plugin, app):
        plugin._manager = MagicMock()
        plugin._manager.status.return_value = {"running": True}
        plugin._stream_url = lambda: ""
        plugin._effective_serial = lambda: "SERIAL123"
        with app.test_request_context("/"):
            resp = plugin.on_api_get(flask.request)
        assert resp.get_json()["led_available"] is True

    def test_status_flags_led_unavailable_without_serial(self, plugin, app):
        plugin._manager = MagicMock()
        plugin._manager.status.return_value = {"running": True}
        plugin._stream_url = lambda: ""
        plugin._effective_serial = lambda: ""
        with app.test_request_context("/"):
            resp = plugin.on_api_get(flask.request)
        assert resp.get_json()["led_available"] is False


class TestEffectiveSerial:
    def test_serial_from_connector(self, plugin):
        info = MagicMock()
        info.serial = "ABC999"
        plugin._detect_connector = lambda: info
        assert plugin._effective_serial() == "ABC999"

    def test_serial_empty_when_none(self, plugin):
        info = MagicMock()
        info.serial = None
        plugin._detect_connector = lambda: info
        assert plugin._effective_serial() == ""

    def test_make_mqtt_uses_effective_values(self, plugin):
        plugin._effective_credentials = lambda: ("10.0.0.5", "code123")
        plugin._effective_serial = lambda: "SER1"
        client = plugin._make_mqtt()
        assert client._hostname == "10.0.0.5"
        assert client._access_code == "code123"
        assert client._serial == "SER1"


class TestThumbnailEndpoint:
    def test_returns_jpeg_and_caches(self, plugin, app, tmp_path):
        plugin._make_ftp = lambda: ThumbSvc(b"jpgbytes")
        plugin.get_plugin_data_folder = lambda: str(tmp_path)
        with app.test_request_context("/?thumb=clip.avi"):
            resp = plugin.on_api_get(flask.request)
        assert resp.mimetype == "image/jpeg"
        assert resp.get_data() == b"jpgbytes"
        # second request must be served from disk cache (no FTP)
        plugin._make_ftp = lambda: (_ for _ in ()).throw(
            AssertionError("FTP should not be hit on cache hit")
        )
        with app.test_request_context("/?thumb=clip.avi"):
            resp2 = plugin.on_api_get(flask.request)
        assert resp2.get_data() == b"jpgbytes"

    def test_missing_thumb_404(self, plugin, app, tmp_path):
        plugin._make_ftp = lambda: ThumbSvc(None)
        plugin.get_plugin_data_folder = lambda: str(tmp_path)
        with app.test_request_context("/?thumb=clip.avi"):
            with pytest.raises(Exception):  # flask.abort(404)
                plugin.on_api_get(flask.request)

    def test_traversal_name_404(self, plugin, app, tmp_path):
        plugin.get_plugin_data_folder = lambda: str(tmp_path)
        with app.test_request_context("/?thumb=" + "../../etc/passwd"):
            with pytest.raises(Exception):  # flask.abort(404)
                plugin.on_api_get(flask.request)


# ---------------------------------------------------------------------------
# local .avi listing + conversion
# ---------------------------------------------------------------------------


class TestLocalAvi:
    def test_list_local_avi(self, plugin, app, tmp_path):
        plugin._settings.global_get_basefolder = lambda _x: str(tmp_path)
        (tmp_path / "a.avi").write_text("x")
        (tmp_path / "b.mp4").write_text("x")  # not an .avi → excluded
        (tmp_path / "c.avi").write_text("xy")
        out = _json(plugin, "list_local_avi", {}, app)
        names = {f["name"] for f in out["files"]}
        assert names == {"a.avi", "c.avi"}

    def test_convert_rejected_while_printing(self, plugin, app, tmp_path):
        plugin._settings.global_get_basefolder = lambda _x: str(tmp_path)
        plugin._printer.is_printing.return_value = True
        out = _json(plugin, "convert_local_avi", {"names": ["a.avi"]}, app)
        assert out == {"ok": False, "reason": "printing"}

    def test_convert_rejected_no_ffmpeg(self, plugin, app, tmp_path):
        plugin._settings.global_get_basefolder = lambda _x: str(tmp_path)
        plugin._make_transcoder = lambda: FakeTranscoder(avail=False)
        out = _json(plugin, "convert_local_avi", {"names": ["a.avi"]}, app)
        assert out["reason"] == "no_ffmpeg"

    def test_convert_empty_names(self, plugin, app, tmp_path):
        plugin._settings.global_get_basefolder = lambda _x: str(tmp_path)
        out = _json(plugin, "convert_local_avi", {"names": []}, app)
        assert out["reason"] == "bad_name"

    def test_convert_batch_converts_and_removes(self, plugin, tmp_path):
        plugin._settings.global_get_basefolder = lambda _x: str(tmp_path)
        (tmp_path / "a.avi").write_text("avi")
        tc = FakeTranscoder(ok=True)
        plugin._make_transcoder = lambda: tc
        pm = plugin._plugin_manager
        plugin._run_convert_batch(["a.avi"])
        assert os.path.exists(tmp_path / "a.mp4")
        assert not os.path.exists(tmp_path / "a.avi")
        msgs = [c.args[1] for c in pm.send_plugin_message.call_args_list]
        assert msgs[-1]["state"] == "batch_done"
        assert msgs[-1]["summary"]["converted"] == 1

    def test_convert_batch_unknown_skipped(self, plugin, tmp_path):
        plugin._settings.global_get_basefolder = lambda _x: str(tmp_path)
        tc = FakeTranscoder(ok=True)
        plugin._make_transcoder = lambda: tc
        pm = plugin._plugin_manager
        plugin._run_convert_batch(["ghost.avi"])
        msgs = [c.args[1] for c in pm.send_plugin_message.call_args_list]
        assert msgs[-1]["summary"]["skipped"] == 1
        assert plugin._ftp_busy is False

    def test_convert_failure_keeps_avi(self, plugin, tmp_path):
        plugin._settings.global_get_basefolder = lambda _x: str(tmp_path)
        (tmp_path / "a.avi").write_text("avi")
        tc = FakeTranscoder(ok=False)
        plugin._make_transcoder = lambda: tc
        pm = plugin._plugin_manager
        plugin._run_convert_batch(["a.avi"])
        assert os.path.exists(tmp_path / "a.avi")  # kept
        msgs = [c.args[1] for c in pm.send_plugin_message.call_args_list]
        err = [m for m in msgs if m.get("state") == "error"]
        assert err and err[0]["reason"] == "transcode_failed"


class TestMovieDoneEvent:
    def test_fires_movie_done_after_copy(
        self, plugin, tmp_path, no_event_manager
    ):
        plugin._settings.get = lambda k: ""
        plugin._settings.get_boolean = lambda k: False  # no transcode
        plugin._settings.global_get_basefolder = lambda _x: str(tmp_path)
        svc = FakeService(listing=["clip.avi"], sizes={"clip.avi": 5})
        plugin._make_ftp = lambda: svc
        plugin._run_batch("copy", ["clip.avi"])
        assert no_event_manager.return_value.fire.called


# ---------------------------------------------------------------------------
# progress throttling + ETA payload
# ---------------------------------------------------------------------------


class ProgressSvc:
    """FakeService whose download streams several progress steps."""

    def __init__(self, steps):
        self._steps = steps  # list of (transferred, total)

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def list_timelapses(self):
        return [{"name": "clip.mp4"}]

    def remote_size(self, name):
        return self._steps[-1][1]

    def download(self, name, dest, *, progress_cb=None):
        for transferred, total in self._steps:
            if progress_cb:
                progress_cb(transferred, total)
        with open(dest, "wb") as fh:
            fh.write(b"x" * self._steps[-1][1])
        return self._steps[-1][1]

    def delete(self, name):
        pass


class TestProgressPayload:
    def test_progress_carries_bytes_and_throttles(self, plugin, tmp_path):
        plugin._settings.get = lambda k: ""
        plugin._settings.get_boolean = lambda k: False  # no transcode
        plugin._settings.global_get_basefolder = lambda _x: str(tmp_path)
        # 0%, 0% (dup → throttled), 50%, 100%
        steps = [(0, 100), (0, 100), (50, 100), (100, 100)]
        plugin._make_ftp = lambda: ProgressSvc(steps)
        pm = plugin._plugin_manager
        plugin._run_batch("copy", ["clip.mp4"])
        msgs = [c.args[1] for c in pm.send_plugin_message.call_args_list]
        progress = [m for m in msgs if m.get("state") == "progress"]
        # the duplicate 0% is throttled out → 3 unique percents
        percents = [m["percent"] for m in progress]
        assert percents == [0, 50, 100]
        # each progress message carries the byte counters for the ETA
        assert progress[1]["transferred"] == 50
        assert progress[1]["total"] == 100
