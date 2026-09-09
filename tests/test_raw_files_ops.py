"""Tests for octoprint_bambucam.raw_files_ops via the plugin mixin."""

# pylint: disable=protected-access
# redefined-outer-name is the standard pytest fixture-injection pattern (a
# fixture and the test param share a name); disabled file-wide as elsewhere.
# pylint: disable=redefined-outer-name

import json
import logging
import os
import time
from unittest.mock import MagicMock

import flask
import pytest

from octoprint_bambucam.raw_files_ops import RawFilesOpsMixin


class Host(RawFilesOpsMixin):
    """Minimal host wiring the render-pipeline mixin to fakes."""

    def __init__(self, data_folder, settings):
        self._logger = logging.getLogger("test.raw_ops")
        self._identifier = "bambucam"
        self._plugin_manager = MagicMock()
        self._settings = settings
        self._data_folder = data_folder
        self._printing = False
        self._movies = []
        self._init_raw_files()

    def get_plugin_data_folder(self):
        """Return the fake plugin data folder (OctoPrint base override)."""
        return self._data_folder

    def _print_active(self):
        return self._printing

    def _fire_movie_done(self, path):
        self._movies.append(path)

    def _sanitized_suffix(self):
        return getattr(self, "_suffix", "")


def _settings(**overrides):
    """Build a MagicMock settings object with render defaults."""
    booleans = {
        "render_enabled": True,
        "render_only_when_idle": True,
    }
    booleans.update(overrides.get("booleans", {}))
    ints = {
        "max_queue_size": 10,
        "stale_lock_timeout": 86400,
        "render_timeout": 10800,
        "ffmpeg_threads": 1,
    }
    ints.update(overrides.get("ints", {}))
    s = MagicMock()
    s.get_boolean = MagicMock(side_effect=lambda k: booleans.get(k[0], False))
    s.get_int = MagicMock(side_effect=lambda k: ints.get(k[0], 0))
    s.get = MagicMock(return_value="")
    s.global_get = MagicMock(return_value="")
    s.global_get_basefolder = MagicMock(
        return_value=overrides.get("timelapse", "/tmp/tl")  # noqa: S108
    )
    return s


@pytest.fixture()
def app():
    """Provide a Flask app context so jsonify works."""
    application = flask.Flask(__name__)
    with application.app_context():
        yield application


@pytest.fixture()
def host(tmp_path):
    """Return a Host with render dirs created and a timelapse folder."""
    tl = tmp_path / "tl"
    tl.mkdir()
    return Host(str(tmp_path / "data"), _settings(timelapse=str(tl)))


def _make_group(host, print_id, chunk_files):
    """Create a ready group directory with chunks + order.json."""
    paths = host._render_paths()
    group = paths.group_dir(print_id)
    os.makedirs(group, exist_ok=True)
    for name in chunk_files:
        with open(os.path.join(group, name), "wb") as fh:
            fh.write(b"x" * 20)
    items = [
        {"file": n, "slot": i, "mdtm": None, "included": True}
        for i, n in enumerate(chunk_files)
    ]
    with open(paths.order_file(print_id), "w", encoding="utf-8") as fh:
        json.dump(items, fh)


@pytest.mark.usefixtures("app")
class TestScanAndList:
    """scan_raw / list_raw_footage return groups + jobs."""

    def test_scan_lists_group(self, host):
        """A created group is reported by scan_raw."""
        _make_group(host, "2026-06-23_1432__gearbox", ["a.avi"])
        resp = json.loads(host.handle_scan_raw().get_data())
        assert resp["ok"] is True
        assert resp["groups"][0]["print_id"] == "2026-06-23_1432__gearbox"

    def test_list_raw_footage(self, host):
        """list_raw_footage returns the cached library + jobs."""
        host._library().scan()
        resp = json.loads(host.handle_list_raw_footage().get_data())
        assert resp["ok"] is True
        assert resp["jobs"] == []


@pytest.mark.usefixtures("app")
class TestStartRender:
    """start_render validates and queues."""

    def test_unknown_group(self, host):
        """A well-formed but unknown group is rejected."""
        host._library().scan()
        resp = json.loads(
            host.handle_start_render(
                {"print_id": "2026-06-23_0000__x"}
            ).get_data()
        )
        assert resp == {"ok": False, "reason": "unknown_group"}

    def test_malformed_print_id(self, host):
        """A print_id failing the canonical grammar is rejected early."""
        resp = json.loads(
            host.handle_start_render({"print_id": "../etc"}).get_data()
        )
        assert resp == {"ok": False, "reason": "bad_id"}

    def test_printing_blocked(self, host):
        """A running print blocks start_render."""
        host._printing = True
        resp = json.loads(
            host.handle_start_render({"print_id": "x"}).get_data()
        )
        assert resp["reason"] == "printing"

    def test_queues_default_chunks(self, host):
        """A ready group with no explicit selection queues all chunks."""
        pid = "2026-06-23_1432__gearbox"
        _make_group(host, pid, ["a.avi", "b.avi"])
        host._library().scan()
        resp = json.loads(
            host.handle_start_render(
                {"print_id": pid, "preset": "fast_720p"}
            ).get_data()
        )
        assert resp["ok"] is True
        host._queue().stop()

    def test_subset_selection_intersects(self, host):
        """A requested chunk subset is intersected with known chunks."""
        pid = "2026-06-23_1432__gearbox"
        _make_group(host, pid, ["a.avi", "b.avi"])
        host._library().scan()
        group = host._library().get(pid)
        chunks = host._select_chunks(group, ["a.avi", "../evil.avi"])
        assert chunks == ["a.avi"]

    def test_empty_selection_rejected(self, host):
        """An empty resolved selection is rejected as no_chunks."""
        pid = "2026-06-23_1432__gearbox"
        _make_group(host, pid, ["a.avi"])
        host._library().scan()
        resp = json.loads(
            host.handle_start_render({"print_id": pid, "chunks": []}).get_data()
        )
        assert resp == {"ok": False, "reason": "no_chunks"}


class TestAutoRender:
    """_auto_render_group queues freshly downloaded groups."""

    @staticmethod
    def _auto_host(tmp_path, preset="fast_720p", **booleans):
        """Host with auto-render enabled and a MagicMock render queue."""
        flags = {
            "auto_render_new_groups": True,
            "render_enabled": True,
            "render_only_when_idle": True,
        }
        flags.update(booleans)
        settings = _settings(booleans=flags)
        settings.get = MagicMock(
            side_effect=lambda k: (preset if k[0] == "default_preset" else "")
        )
        host = Host(str(tmp_path / "data"), settings)
        host._render_queue = MagicMock()
        host._render_queue.enqueue = MagicMock(
            return_value={"ok": True, "jobid": "j1"}
        )
        return host

    def test_queues_with_default_preset(self, tmp_path):
        """A ready group is queued with the configured default preset."""
        host = self._auto_host(tmp_path, preset="quality_1080p")
        pid = "2026-06-23_1432__gearbox"
        _make_group(host, pid, ["a.avi", "b.avi"])
        host._auto_render_group(pid)
        host._render_queue.enqueue.assert_called_once_with(
            pid, "quality_1080p", ["a.avi", "b.avi"]
        )

    def test_disabled_setting_no_enqueue(self, tmp_path):
        """With auto_render_new_groups off nothing is queued."""
        host = self._auto_host(tmp_path, auto_render_new_groups=False)
        pid = "2026-06-23_1432__gearbox"
        _make_group(host, pid, ["a.avi"])
        host._auto_render_group(pid)
        host._render_queue.enqueue.assert_not_called()

    def test_render_disabled_no_enqueue(self, tmp_path):
        """With render_enabled off nothing is queued."""
        host = self._auto_host(tmp_path, render_enabled=False)
        pid = "2026-06-23_1432__gearbox"
        _make_group(host, pid, ["a.avi"])
        host._auto_render_group(pid)
        host._render_queue.enqueue.assert_not_called()

    def test_invalid_preset_falls_back(self, tmp_path):
        """An unknown default_preset falls back to the built-in default."""
        host = self._auto_host(tmp_path, preset="bogus")
        pid = "2026-06-23_1432__gearbox"
        _make_group(host, pid, ["a.avi"])
        host._auto_render_group(pid)
        args = host._render_queue.enqueue.call_args.args
        assert args[1] == "fast_720p"

    def test_incomplete_group_skipped(self, tmp_path):
        """A group that is not chunks_ready is not queued."""
        host = self._auto_host(tmp_path)
        pid = "2026-06-23_1432__gearbox"
        _make_group(host, pid, ["a.avi"])
        os.remove(host._render_paths().order_file(pid))
        host._auto_render_group(pid)
        host._render_queue.enqueue.assert_not_called()

    def test_unknown_group_skipped(self, tmp_path):
        """A print_id without a group directory is ignored."""
        host = self._auto_host(tmp_path)
        host._auto_render_group("2026-06-23_0000__nothing")
        host._render_queue.enqueue.assert_not_called()

    def test_enqueue_failure_does_not_raise(self, tmp_path):
        """A rejected enqueue (e.g. queue_full) is logged, not raised."""
        host = self._auto_host(tmp_path)
        host._render_queue.enqueue.return_value = {
            "ok": False,
            "reason": "queue_full",
        }
        pid = "2026-06-23_1432__gearbox"
        _make_group(host, pid, ["a.avi"])
        host._auto_render_group(pid)

    def test_internal_error_swallowed(self, tmp_path):
        """An exception inside the hook must not escape into the harvest."""
        host = self._auto_host(tmp_path)
        host._render_queue.enqueue.side_effect = RuntimeError("boom")
        pid = "2026-06-23_1432__gearbox"
        _make_group(host, pid, ["a.avi"])
        host._auto_render_group(pid)


class TestRunRenderJob:
    """_run_render_job builds the worker with the configured limits."""

    def test_passes_timeout_and_threads(self, tmp_path, monkeypatch):
        """The worker gets ``render_timeout``/``ffmpeg_threads`` from settings.

        Regression: the worker was built without ``timeout=``, so it fell back
        to the 30-min transcode default and long quality renders were killed
        as ``ffmpeg exceeded the time limit`` well before ``render_timeout``.
        """
        host = Host(
            str(tmp_path / "data"),
            _settings(
                timelapse=str(tmp_path),
                ints={"render_timeout": 9000, "ffmpeg_threads": 3},
            ),
        )
        captured = {}

        class FakeWorker:
            """Capture the constructor kwargs and return a fixed output."""

            def __init__(self, *_args, **kwargs):
                captured.update(kwargs)

            def run(self, _job, _cancel, _progress_cb):
                """Pretend the render produced a file."""
                return "/tmp/out.mp4"  # noqa: S108

        monkeypatch.setattr(
            "octoprint_bambucam.raw_files_ops.RenderWorker", FakeWorker
        )
        out = host._run_render_job(
            {"jobid": "j", "print_id": "p", "preset": "fast_720p"},
            MagicMock(),
            lambda *a: None,
        )
        assert out == "/tmp/out.mp4"  # noqa: S108
        assert captured["options"].timeout == 9000
        assert captured["options"].threads == 3

    def test_gcode_thumb_off_by_default(self, tmp_path, monkeypatch):
        """With raw_thumb_from_gcode off the worker gets gcode_thumb=None."""
        host = Host(str(tmp_path / "data"), _settings(timelapse=str(tmp_path)))
        captured = {}

        class FakeWorker:
            def __init__(self, *_a, **kw):
                captured.update(kw)

            def run(self, *_a):
                return "/tmp/out.mp4"  # noqa: S108

        monkeypatch.setattr(
            "octoprint_bambucam.raw_files_ops.RenderWorker", FakeWorker
        )
        host._run_render_job(
            {
                "jobid": "j",
                "print_id": "2026-06-23_1432__x",
                "preset": "fast_720p",
            },
            MagicMock(),
            lambda *a: None,
        )
        assert captured["options"].gcode_thumb is None

    def test_gcode_thumb_resolved_when_on(self, tmp_path, monkeypatch):
        """When on, the print-id stem resolves the connector preview PNG."""
        data = tmp_path / "data"
        (data / "bambucam").mkdir(parents=True)
        plate_dir = data / "bambu_connector" / "thumbs" / "gearbox.gcode.3mf"
        plate_dir.mkdir(parents=True)
        (plate_dir / "plate_1.png").write_bytes(b"png")
        host = Host(
            str(data / "bambucam"),
            _settings(
                timelapse=str(tmp_path),
                booleans={"raw_thumb_from_gcode": True},
            ),
        )
        captured = {}

        class FakeWorker:
            def __init__(self, *_a, **kw):
                captured.update(kw)

            def run(self, *_a):
                return "/tmp/out.mp4"  # noqa: S108

        monkeypatch.setattr(
            "octoprint_bambucam.raw_files_ops.RenderWorker", FakeWorker
        )
        host._run_render_job(
            {
                "jobid": "j",
                "print_id": "2026-06-23_1432__gearbox",
                "preset": "fast_720p",
            },
            MagicMock(),
            lambda *a: None,
        )
        assert captured["options"].gcode_thumb.endswith("plate_1.png")

    def test_threads_zero_means_all_cores(self, tmp_path, monkeypatch):
        """``ffmpeg_threads`` 0 (all cores) must reach the worker as 0.

        Regression: ``get_int(...) or 1`` turned the intended 0 into 1, capping
        the encode to a single core despite the user selecting "all cores".
        """
        host = Host(
            str(tmp_path / "data"),
            _settings(timelapse=str(tmp_path), ints={"ffmpeg_threads": 0}),
        )
        captured = {}

        class FakeWorker:
            """Capture the constructor kwargs and return a fixed output."""

            def __init__(self, *_args, **kwargs):
                captured.update(kwargs)

            def run(self, _job, _cancel, _progress_cb):
                """Pretend the render produced a file."""
                return "/tmp/out.mp4"  # noqa: S108

        monkeypatch.setattr(
            "octoprint_bambucam.raw_files_ops.RenderWorker", FakeWorker
        )
        host._run_render_job(
            {"jobid": "j", "print_id": "p", "preset": "fast_720p"},
            MagicMock(),
            lambda *a: None,
        )
        assert captured["options"].threads == 0


@pytest.mark.usefixtures("app")
class TestCancelAndDelete:
    """cancel_render / delete_group."""

    def test_cancel_bad_jobid(self, host):
        """Cancelling without a jobid is rejected."""
        resp = json.loads(host.handle_cancel_render({}).get_data())
        assert resp == {"ok": False, "reason": "bad_jobid"}

    def test_delete_group_removes_from_disk(self, host):
        """Discard removes the group directly — no trash copy is kept."""
        pid = "2026-06-23_1432__gearbox"
        _make_group(host, pid, ["a.avi"])
        host._library().scan()
        resp = json.loads(
            host.handle_delete_group({"print_id": pid}).get_data()
        )
        assert resp["ok"] is True
        assert not os.path.isdir(host._render_paths().group_dir(pid))
        # gone for good — not parked in trash/
        assert not os.path.isdir(
            os.path.join(host._render_paths().trash_dir, pid)
        )

    def test_delete_unknown(self, host):
        """Deleting an unknown group is rejected."""
        resp = json.loads(
            host.handle_delete_group(
                {"print_id": "2026-06-23_0000__x"}
            ).get_data()
        )
        assert resp == {"ok": False, "reason": "unknown_group"}

    def test_delete_blocked_while_printing(self, host):
        """Deleting is blocked while a print runs."""
        host._printing = True
        resp = json.loads(
            host.handle_delete_group({"print_id": "x"}).get_data()
        )
        assert resp["reason"] == "printing"


@pytest.mark.usefixtures("app")
class TestStatusAndThumb:
    """render_status / ffprobe_status / raw thumbnail."""

    def test_render_status(self, host):
        """render_status returns the (empty) job list."""
        resp = json.loads(host.handle_render_status().get_data())
        assert resp == {"ok": True, "jobs": []}

    def test_ffprobe_status(self, host):
        """ffprobe_status reports configuration."""
        resp = json.loads(host.handle_ffprobe_status().get_data())
        assert "ffprobe" in resp

    def test_render_ffmpeg_status_missing(self, host):
        """An unresolved render ffmpeg reports not executable."""
        resp = json.loads(host.handle_render_ffmpeg_status().get_data())
        assert resp["ok"] is True
        assert resp["ffmpeg"]["executable"] is False

    def test_render_ffmpeg_status_executable(self, host, tmp_path):
        """A runnable ffmpeg_path override reports executable."""
        fake = tmp_path / "ffmpeg"
        fake.write_text("#!/bin/sh\n")
        fake.chmod(0o755)
        host._settings.get = MagicMock(
            side_effect=lambda k: (str(fake) if k[0] == "ffmpeg_path" else "")
        )
        resp = json.loads(host.handle_render_ffmpeg_status().get_data())
        assert resp["ffmpeg"]["executable"] is True
        assert resp["ffmpeg"]["path"] == str(fake)

    def test_raw_thumb_404_when_missing(self, host):
        """A missing thumbnail aborts with 404."""
        with pytest.raises(Exception):
            host.handle_raw_thumb("2026-06-23_1432__gearbox")

    def test_raw_thumb_served(self, host):
        """An existing thumbnail is served as JPEG."""
        pid = "2026-06-23_1432__gearbox"
        thumb = host._render_paths().thumb_file(pid)
        with open(thumb, "wb") as fh:
            fh.write(b"jpegdata")
        resp = host.handle_raw_thumb(pid)
        assert resp.mimetype == "image/jpeg"


def _rendered_group(host, print_id, age_days=0):
    """Create a rendered group whose marker is ``age_days`` old."""
    _make_group(host, print_id, ["a.avi"])
    marker = host._render_paths().rendered_marker(print_id)
    with open(marker, "w", encoding="utf-8") as fh:
        json.dump({"rendered_at": "x", "output": "o.mp4"}, fh)
    if age_days:
        old = time.time() - age_days * 86400
        os.utime(marker, (old, old))


def _retention_settings(host, days, to_trash=True):
    """Wire retention days and the move_to_trash flag on the fake settings."""
    ints = {
        "chunks_retention_days": days,
        "max_queue_size": 10,
        "stale_lock_timeout": 86400,
    }
    booleans = {
        "render_enabled": True,
        "render_only_when_idle": True,
        "move_to_trash": to_trash,
    }
    host._settings.get_int = MagicMock(side_effect=lambda k: ints.get(k[0], 0))
    host._settings.get_boolean = MagicMock(
        side_effect=lambda k: booleans.get(k[0], False)
    )


class TestRetention:
    """_retention_cleanup ages out rendered groups and old trash."""

    def test_old_rendered_group_trashed(self, host):
        pid = "2026-06-01_1000__old"
        _rendered_group(host, pid, age_days=10)
        host._library().scan()
        _retention_settings(host, days=7)
        host._retention_cleanup()
        paths = host._render_paths()
        assert not os.path.isdir(paths.group_dir(pid))
        assert os.path.isdir(os.path.join(paths.trash_dir, pid))
        assert host._library().get(pid) is None

    def test_old_rendered_group_hard_deleted(self, host):
        pid = "2026-06-01_1000__old"
        _rendered_group(host, pid, age_days=10)
        _retention_settings(host, days=7, to_trash=False)
        host._retention_cleanup()
        paths = host._render_paths()
        assert not os.path.isdir(paths.group_dir(pid))
        assert not os.path.isdir(os.path.join(paths.trash_dir, pid))

    def test_fresh_rendered_group_kept(self, host):
        pid = "2026-07-04_1429__fresh"
        _rendered_group(host, pid, age_days=1)
        _retention_settings(host, days=7)
        host._retention_cleanup()
        assert os.path.isdir(host._render_paths().group_dir(pid))

    def test_unrendered_group_never_touched(self, host):
        pid = "2026-06-01_1000__unrendered"
        _make_group(host, pid, ["a.avi"])
        old = time.time() - 30 * 86400
        group = host._render_paths().group_dir(pid)
        os.utime(group, (old, old))
        _retention_settings(host, days=7)
        host._retention_cleanup()
        assert os.path.isdir(group)

    def test_old_trash_purged(self, host):
        paths = host._render_paths()
        entry = os.path.join(paths.trash_dir, "2026-06-01_1000__gone")
        os.makedirs(entry)
        old = time.time() - 30 * 86400
        os.utime(entry, (old, old))
        _retention_settings(host, days=7)
        host._retention_cleanup()
        assert not os.path.isdir(entry)

    def test_zero_days_disables_retention(self, host):
        pid = "2026-06-01_1000__old"
        _rendered_group(host, pid, age_days=365)
        _retention_settings(host, days=0)
        host._retention_cleanup()
        assert os.path.isdir(host._render_paths().group_dir(pid))

    def test_stop_pipeline_cancels_timer(self, host):
        timer = MagicMock()
        host._retention_timer = timer
        host.stop_render_pipeline()
        timer.cancel.assert_called_once()
        assert host._retention_timer is None


class TestRenderOutputName:
    """_render_output_name builds <stem>__<date><suffix>_<preset>_RAW.mp4."""

    def test_pattern_with_suffix_and_preset(self, host):
        host._suffix = "_A1mini"
        name = host._render_output_name(
            "2026-07-04_1429__OctoPrint-Upload_red", "original"
        )
        assert (
            name
            == "OctoPrint-Upload_red__2026-07-04_1429_A1mini_original_RAW.mp4"
        )

    def test_pattern_without_suffix(self, host):
        name = host._render_output_name("2026-07-04_1429__red", "fast_720p")
        assert name == "red__2026-07-04_1429_fast_720p_RAW.mp4"

    def test_unsplittable_print_id_falls_back(self, host):
        assert host._render_output_name("oddid", "p") == "oddid_p_RAW.mp4"


class TestPipelineLifecycle:
    """start/stop pipeline and gate."""

    def test_gate_open_when_idle(self, host):
        """The render gate is open when the printer is idle."""
        assert host._render_gate_open() is True
        host._printing = True
        assert host._render_gate_open() is False

    def test_start_pipeline_disabled(self, tmp_path):
        """A disabled render feature does not build the queue."""
        s = _settings(booleans={"render_enabled": False})
        h = Host(str(tmp_path / "d"), s)
        h.start_render_pipeline()
        assert h._render_queue is None

    def test_thumb_generation_skips_without_ffmpeg(self, host):
        """Raw-thumb generation is a no-op without ffmpeg configured."""
        pid = "2026-06-23_1432__gearbox"
        _make_group(host, pid, ["a.avi"])
        host._library().scan()
        group = host._library().get(pid)
        host._make_raw_thumb(group)  # ffmpeg path empty → no crash, no thumb
        assert not os.path.isfile(host._render_paths().thumb_file(pid))

    def test_thumb_generation_publishes(self, tmp_path, monkeypatch):
        """With ffmpeg configured and succeeding, a thumbnail is published."""
        tl = tmp_path / "tl"
        tl.mkdir()
        settings = _settings(timelapse=str(tl))
        settings.get = MagicMock(
            side_effect=lambda k: "/bin/ffmpeg" if k[0] == "ffmpeg_path" else ""
        )
        host = Host(str(tmp_path / "data"), settings)
        pid = "2026-06-23_1432__gearbox"
        _make_group(host, pid, ["a.avi"])
        host._library().scan()
        group = host._library().get(pid)

        def fake_run(cmd):
            with open(cmd[-1], "wb") as fh:
                fh.write(b"jpeg")
            return True

        monkeypatch.setattr(host, "_run_thumb_cmd", fake_run)
        host._make_raw_thumb(group)
        thumb = host._render_paths().thumb_file(pid)
        assert thumb is not None
        assert os.path.isfile(thumb)

    def test_thumb_cmd_forces_image2_muxer(self, tmp_path, monkeypatch):
        """The thumb command forces -f image2 for the .part temp output.

        ffmpeg picks the output muxer from the file extension; without the
        explicit format the ".part" temp name makes every thumb fail.
        """
        settings = _settings()
        settings.get = MagicMock(
            side_effect=lambda k: "/bin/ffmpeg" if k[0] == "ffmpeg_path" else ""
        )
        host = Host(str(tmp_path / "data"), settings)
        pid = "2026-06-23_1432__gearbox"
        _make_group(host, pid, ["a.avi"])
        host._library().scan()
        seen = {}

        def fake_run(cmd):
            seen["cmd"] = cmd
            return False

        monkeypatch.setattr(host, "_run_thumb_cmd", fake_run)
        host._make_raw_thumb(host._library().get(pid))
        cmd = seen["cmd"]
        assert cmd[-3:-1] == ["-f", "image2"]
        assert cmd[-1].endswith(".part")

    def test_thumb_failure_cleans_part(self, tmp_path, monkeypatch):
        """A failed thumb run leaves no .part temp and no thumbnail."""
        settings = _settings()
        settings.get = MagicMock(
            side_effect=lambda k: "/bin/ffmpeg" if k[0] == "ffmpeg_path" else ""
        )
        host = Host(str(tmp_path / "data"), settings)
        pid = "2026-06-23_1432__gearbox"
        _make_group(host, pid, ["a.avi"])
        host._library().scan()

        def fake_run(cmd):
            with open(cmd[-1], "wb") as fh:
                fh.write(b"partial")
            return False

        monkeypatch.setattr(host, "_run_thumb_cmd", fake_run)
        host._make_raw_thumb(host._library().get(pid))
        thumb = host._render_paths().thumb_file(pid)
        assert not os.path.isfile(thumb)
        assert not os.path.isfile(thumb + ".part")

    def test_ensure_thumbs_skips_present(self, host):
        """_ensure_thumbs skips groups that already have a thumbnail."""
        pid = "2026-06-23_1432__gearbox"
        _make_group(host, pid, ["a.avi"])
        groups = host._library().scan()
        groups[0]["has_thumb"] = True
        host._ensure_thumbs(groups)  # no ffmpeg invoked, no crash


@pytest.mark.usefixtures("app")
class TestInterruptedHarvestRecovery:
    """Startup clears .part temps a killed harvest left behind."""

    @staticmethod
    def _write(path, size=40):
        with open(path, "wb") as fh:
            fh.write(b"x" * size)

    def test_part_only_group_discarded(self, host):
        """A group with nothing but a .part is removed entirely."""
        pid = "2026-09-09_1817__cover"
        group = host._render_paths().group_dir(pid)
        os.makedirs(group, exist_ok=True)
        self._write(os.path.join(group, "ipcam-record.1.avi.part"))
        host._recover_interrupted_harvests()
        assert not os.path.isdir(group)

    def test_part_beside_chunk_only_clears_temp(self, host):
        """A group holding a real chunk survives; only the .part goes."""
        pid = "2026-09-09_1913__rocket"
        _make_group(host, pid, ["a.avi"])
        group = host._render_paths().group_dir(pid)
        self._write(os.path.join(group, "ipcam-record.2.avi.part"))
        host._recover_interrupted_harvests()
        assert os.path.isdir(group)
        assert os.path.isfile(os.path.join(group, "a.avi"))
        assert not os.path.isfile(
            os.path.join(group, "ipcam-record.2.avi.part")
        )

    def test_clean_group_untouched(self, host):
        """A group without temps is left exactly as it is."""
        pid = "2026-09-09_1913__rocket"
        _make_group(host, pid, ["a.avi"])
        group = host._render_paths().group_dir(pid)
        before = sorted(os.listdir(group))
        host._recover_interrupted_harvests()
        assert sorted(os.listdir(group)) == before

    def test_recovery_runs_on_pipeline_start(self, host):
        """start_render_pipeline performs the recovery before scanning."""
        pid = "2026-09-09_1817__cover"
        group = host._render_paths().group_dir(pid)
        os.makedirs(group, exist_ok=True)
        self._write(os.path.join(group, "ipcam-record.1.avi.part"))
        host.start_render_pipeline()
        try:
            assert not os.path.isdir(group)
            # and the discarded group is not reported to the UI
            assert host._library().groups() == []
        finally:
            host.stop_render_pipeline()

    def test_forgets_discarded_group_from_cache(self, host):
        """A cached group discarded at startup drops out of the library."""
        pid = "2026-09-09_1817__cover"
        group = host._render_paths().group_dir(pid)
        os.makedirs(group, exist_ok=True)
        self._write(os.path.join(group, "ipcam-record.1.avi.part"))
        host._library().scan()
        assert host._library().get(pid) is not None
        host._recover_interrupted_harvests()
        assert host._library().get(pid) is None
