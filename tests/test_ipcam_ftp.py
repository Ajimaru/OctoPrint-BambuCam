"""Tests for octoprint_bambucam.ipcam_ftp."""

# pylint: disable=protected-access
# redefined-outer-name is the standard pytest fixture-injection pattern (a
# fixture and the test param share a name); disabled file-wide as elsewhere.
# pylint: disable=redefined-outer-name

import logging
import os

import pytest

from octoprint_bambucam.ipcam_ftp import IPCAM_DIR, BambuIpcamFtp, _parse_slot


@pytest.fixture()
def logger():
    """Return a test-scoped logger."""
    return logging.getLogger("test.ipcam_ftp")


class FakeFTP:
    """Minimal FTP stand-in serving an /ipcam MLSD listing."""

    def __init__(self):
        self.cwd_path = None
        self.timeout = 20  # mirrors ftplib.FTP.timeout (widened per download)

    def cwd(self, path):
        """Record the directory the service changed into."""
        self.cwd_path = path

    def size(self, _name):
        """Return a canned chunk size."""
        return 10

    def retrbinary(self, _cmd, callback):
        """Feed one canned chunk to the callback."""
        callback(b"x" * 10)

    def mlsd(self):
        """Yield two /ipcam chunks and one non-video entry."""
        yield (
            "ipcam-record.20260623.5.avi",
            {"type": "file", "size": "129000000", "modify": "20260623143200"},
        )
        yield (
            "ipcam-record.20260623.6.avi",
            {"type": "file", "size": "129000000", "modify": "20260623143300"},
        )
        yield ("index", {"type": "file", "size": "4"})

    def quit(self):
        """No-op quit."""

    def close(self):
        """No-op close."""


@pytest.fixture()
def patched_ipcam(monkeypatch):
    """Return a BambuIpcamFtp whose _connect yields a FakeFTP."""

    def install():
        fake = FakeFTP()
        monkeypatch.setattr(BambuIpcamFtp, "_connect", lambda self: fake)
        return fake

    return install


class TestListIpcam:
    """list_ipcam lists /ipcam and annotates the slot."""

    def test_lists_with_slot(self, logger, patched_ipcam):
        """Each chunk gets a parsed slot; non-video is filtered."""
        patched_ipcam()
        with BambuIpcamFtp(logger, "h", "c") as svc:
            files = svc.list_ipcam()
        assert len(files) == 2
        assert files[0]["slot"] == 5
        assert files[1]["slot"] == 6
        assert all(f["name"].endswith(".avi") for f in files)

    def test_lists_dir_is_ipcam(self, logger, patched_ipcam):
        """The service changes into /ipcam, not /timelapse."""
        fake = patched_ipcam()
        with BambuIpcamFtp(logger, "h", "c") as svc:
            svc.list_ipcam()
        assert fake.cwd_path == IPCAM_DIR

    def test_download_cwds_into_ipcam(self, logger, patched_ipcam, tmp_path):
        """A fresh-session download cwds into /ipcam before RETR (550 fix)."""
        fake = patched_ipcam()
        dest = str(tmp_path / "chunk.avi")
        with BambuIpcamFtp(logger, "h", "c") as svc:
            svc.download("ipcam-record.20260623.5.avi", dest)
        assert fake.cwd_path == IPCAM_DIR
        assert os.path.isfile(dest)


class TestParseSlot:
    """_parse_slot extracts the ring-buffer slot number."""

    def test_parses_slot(self):
        """A valid ring-buffer name yields its slot."""
        assert _parse_slot("ipcam-record.20260623.42.avi") == 42

    def test_no_match_is_none(self):
        """A non-matching name yields None."""
        assert _parse_slot("index") is None
        assert _parse_slot("") is None
