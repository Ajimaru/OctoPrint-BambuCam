"""Tests for the Raw Files render API routing on BambucamPlugin."""

# pylint: disable=protected-access,redefined-outer-name
# pylint: disable=missing-function-docstring,missing-class-docstring

from unittest.mock import MagicMock, patch

import flask
import pytest


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


@pytest.fixture()
def render_plugin(plugin, tmp_path):
    """Plugin wired with a real data folder for the render pipeline."""
    plugin._manager = MagicMock()
    plugin.get_plugin_data_folder = MagicMock(return_value=str(tmp_path))
    plugin._settings.get = MagicMock(return_value="")
    plugin._settings.global_get = MagicMock(return_value="")
    plugin._settings.global_get_basefolder = MagicMock(
        return_value=str(tmp_path / "tl")
    )
    (tmp_path / "tl").mkdir()
    return plugin


def _json(plugin, command, data, app):
    with app.test_request_context():
        resp = plugin.on_api_command(command, data)
        return resp.get_json()


class TestRenderApiRegistration:
    def test_render_commands_registered(self, plugin):
        cmds = plugin.get_api_commands()
        for name in (
            "list_raw_footage",
            "scan_raw",
            "harvest_ipcam",
            "pipeline_status",
            "start_render",
            "cancel_render",
            "delete_group",
            "delete_chunks",
            "render_status",
            "ffprobe_status",
        ):
            assert name in cmds


class TestRenderApiRouting:
    def test_scan_raw(self, render_plugin, app):
        result = _json(render_plugin, "scan_raw", {}, app)
        assert result["ok"] is True
        assert result["groups"] == []

    def test_list_raw_footage(self, render_plugin, app):
        result = _json(render_plugin, "list_raw_footage", {}, app)
        assert result["ok"] is True

    def test_render_status(self, render_plugin, app):
        result = _json(render_plugin, "render_status", {}, app)
        assert result == {"ok": True, "jobs": []}

    def test_ffprobe_status(self, render_plugin, app):
        result = _json(render_plugin, "ffprobe_status", {}, app)
        assert "ffprobe" in result

    def test_pipeline_status(self, render_plugin, app):
        result = _json(render_plugin, "pipeline_status", {}, app)
        assert result["ok"] is True
        assert result["pipeline"]["busy"] is False
        assert "chunk_total" in result["pipeline"]

    def test_render_ffmpeg_status(self, render_plugin, app):
        result = _json(render_plugin, "render_ffmpeg_status", {}, app)
        assert result["ok"] is True
        assert "executable" in result["ffmpeg"]

    def test_cancel_render_bad_jobid(self, render_plugin, app):
        result = _json(render_plugin, "cancel_render", {}, app)
        assert result == {"ok": False, "reason": "bad_jobid"}

    def test_start_render_unknown_group(self, render_plugin, app):
        result = _json(
            render_plugin,
            "start_render",
            {"print_id": "2026-06-23_0000__x"},
            app,
        )
        assert result["reason"] == "unknown_group"

    def test_start_render_malformed_print_id(self, render_plugin, app):
        result = _json(render_plugin, "start_render", {"print_id": "../x"}, app)
        assert result["reason"] == "bad_id"

    def test_harvest_ipcam_blocked_while_printing(self, render_plugin, app):
        render_plugin._printer.is_printing = MagicMock(return_value=True)
        result = _json(render_plugin, "harvest_ipcam", {}, app)
        assert result["reason"] == "printing"

    def test_delete_group_unknown(self, render_plugin, app):
        result = _json(
            render_plugin,
            "delete_group",
            {"print_id": "2026-06-23_0000__x"},
            app,
        )
        assert result["reason"] == "unknown_group"


def _seed_group(plugin, print_id, chunk_files):
    """Create a real group dir + order.json and load it into the library."""
    import json
    import os

    paths = plugin._render_paths()
    group = paths.group_dir(print_id)
    os.makedirs(group, exist_ok=True)
    for name in chunk_files:
        with open(os.path.join(group, name), "wb") as fh:
            fh.write(b"x" * 100)
    items = [
        {"file": n, "slot": i, "mdtm": None, "included": True}
        for i, n in enumerate(chunk_files)
    ]
    with open(paths.order_file(print_id), "w", encoding="utf-8") as fh:
        json.dump(items, fh)
    plugin._library().scan()
    return group


class TestDeleteChunks:
    PID = "2026-06-23_1200__x"

    def test_delete_chunks_bad_id(self, render_plugin, app):
        result = _json(
            render_plugin, "delete_chunks", {"print_id": "../x"}, app
        )
        assert result["reason"] == "bad_id"

    def test_delete_chunks_unknown_group(self, render_plugin, app):
        result = _json(
            render_plugin,
            "delete_chunks",
            {"print_id": self.PID, "chunks": ["a.avi"]},
            app,
        )
        assert result["reason"] == "unknown_group"

    def test_delete_chunks_blocked_while_printing(self, render_plugin, app):
        render_plugin._printer.is_printing = MagicMock(return_value=True)
        result = _json(
            render_plugin,
            "delete_chunks",
            {"print_id": self.PID, "chunks": ["a.avi"]},
            app,
        )
        assert result["reason"] == "printing"

    def test_delete_removes_slot_pair_together(self, render_plugin, app):
        import os

        group = _seed_group(
            render_plugin,
            self.PID,
            [
                "ipcam-record.20260623.0.avi",
                "ipcam-record.20260623.1.avi",
                "ipcam-record.20260624.0.avi",
            ],
        )
        result = _json(
            render_plugin,
            "delete_chunks",
            {
                "print_id": self.PID,
                "chunks": ["ipcam-record.20260623.0.avi"],
            },
            app,
        )
        assert result["ok"] is True
        # Both slots of the 20260623 recording are gone; the other stays.
        assert not os.path.exists(
            os.path.join(group, "ipcam-record.20260623.0.avi")
        )
        assert not os.path.exists(
            os.path.join(group, "ipcam-record.20260623.1.avi")
        )
        assert os.path.exists(
            os.path.join(group, "ipcam-record.20260624.0.avi")
        )

    def test_delete_rewrites_order_json(self, render_plugin, app):
        import json
        import os

        _seed_group(
            render_plugin,
            self.PID,
            ["ipcam-record.20260623.0.avi", "ipcam-record.20260624.0.avi"],
        )
        _json(
            render_plugin,
            "delete_chunks",
            {
                "print_id": self.PID,
                "chunks": ["ipcam-record.20260623.0.avi"],
            },
            app,
        )
        order_file = render_plugin._render_paths().order_file(self.PID)
        with open(order_file, encoding="utf-8") as fh:
            order = json.load(fh)
        names = {os.path.basename(i["file"]) for i in order}
        assert names == {"ipcam-record.20260624.0.avi"}

    def test_delete_last_chunk_forgets_group(self, render_plugin, app):
        _seed_group(render_plugin, self.PID, ["ipcam-record.20260623.0.avi"])
        result = _json(
            render_plugin,
            "delete_chunks",
            {
                "print_id": self.PID,
                "chunks": ["ipcam-record.20260623.0.avi"],
            },
            app,
        )
        assert result["ok"] is True
        assert render_plugin._library().get(self.PID) is None

    def test_delete_foreign_name_ignored(self, render_plugin, app):
        _seed_group(render_plugin, self.PID, ["ipcam-record.20260623.0.avi"])
        result = _json(
            render_plugin,
            "delete_chunks",
            {"print_id": self.PID, "chunks": ["/etc/passwd"]},
            app,
        )
        assert result["reason"] == "no_chunks"


class TestRawThumbGet:
    def test_raw_thumb_rejects_bad_id(self, render_plugin, app):
        with app.test_request_context("/?raw_thumb=../escape"):
            with pytest.raises(Exception):
                render_plugin.on_api_get(flask.request)

    def test_event_dispatch_does_not_crash(self, render_plugin):
        # exercise the on_event fan-out (autosync + ipcam) with a no-op event
        render_plugin._settings.get_boolean = MagicMock(return_value=False)
        render_plugin.on_event("SomeEvent", {})


class TestStartupShutdown:
    def test_startup_recovers_pipeline(self, render_plugin):
        render_plugin._settings.get_boolean = MagicMock(return_value=True)
        render_plugin._settings.get_int = MagicMock(return_value=86400)
        render_plugin.start_render_pipeline()
        render_plugin.stop_render_pipeline()
