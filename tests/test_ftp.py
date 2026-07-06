"""Tests for octoprint_bambucam.ftp.BambuTimelapseFtp."""

# pylint: disable=protected-access,redefined-outer-name,too-few-public-methods

import ftplib
import logging
import os
import socket
import ssl
from unittest import mock

import pytest

from octoprint_bambucam import ftp as ftp_mod
from octoprint_bambucam.ftp import (
    MAX_THUMBNAIL_BYTES,
    BambuTimelapseFtp,
    FtpError,
    ImplicitFTP_TLS,
)


@pytest.fixture()
def logger():
    """Return a throwaway logger for the service under test."""
    return logging.getLogger("test.ftp")


class FakeFTP:
    """Stand-in for ImplicitFTP_TLS: records calls, serves canned data."""

    def __init__(
        self,
        *,
        mlsd_data=None,
        nlst_data=None,
        sizes=None,
        retr_chunks=None,
        connect_count=None,
    ):
        self.host = "printer"
        self._mlsd = mlsd_data
        self._nlst = nlst_data or []
        self._sizes = sizes or {}
        self._retr = retr_chunks or {}
        self.deleted = []
        self.cwd_path = None
        self.quit_called = False
        # shared counter so a batch can assert one connect for N files
        self._connect_count = connect_count

    def connect(self, **_kwargs):
        """Record the connect call (incrementing the shared counter)."""
        if self._connect_count is not None:
            self._connect_count[0] += 1

    def login(self, *_a):
        """No-op login."""

    def prot_p(self):
        """No-op switch to a protected data channel."""

    def cwd(self, path):
        """Record the requested working directory."""
        self.cwd_path = path

    def mlsd(self):
        """Yield canned MLSD entries or raise if none were configured."""
        if self._mlsd is None:
            raise ftplib.error_perm("500 MLSD not supported")
        return iter(self._mlsd)

    def nlst(self):
        """Return the canned NLST listing."""
        return list(self._nlst)

    def size(self, name):
        """Return a canned file size or raise error_perm if unknown."""
        if name in self._sizes:
            return self._sizes[name]
        raise ftplib.error_perm("550")

    def retrbinary(self, cmd, callback):
        """Feed canned chunks for the requested RETR command to callback."""
        name = cmd.split(" ", 1)[1]
        for chunk in self._retr.get(name, []):
            callback(chunk)

    def delete(self, name):
        """Record a deleted path."""
        self.deleted.append(name)

    def quit(self):
        """Record a graceful quit."""
        self.quit_called = True

    def close(self):
        """No-op close."""


@pytest.fixture()
def patch_ftp(monkeypatch):
    """Patch the service's _connect to return a provided FakeFTP."""

    def _install(fake):
        def fake_connect(_self):
            fake.connect()
            return fake

        monkeypatch.setattr(BambuTimelapseFtp, "_connect", fake_connect)
        return fake

    return _install


# ---------------------------------------------------------------------------
# ImplicitFTP_TLS
# ---------------------------------------------------------------------------


class TestImplicitFTP_TLS:  # pylint: disable=invalid-name
    """Cover the implicit-TLS socket wrapping behaviour."""

    def test_sock_wraps_plain_socket(self):
        """A plain socket assigned to .sock is wrapped via the TLS context."""
        client = ImplicitFTP_TLS.__new__(ImplicitFTP_TLS)
        client._sock = None
        wrapped = object()
        ctx = ssl.create_default_context()
        ctx.wrap_socket = lambda s, *a, **k: wrapped  # type: ignore
        client.context = ctx
        client.sock = socket.socket()
        assert client.sock is wrapped

    def test_sock_passes_through_sslsocket(self):
        """An already-wrapped SSLSocket is stored without re-wrapping."""
        client = ImplicitFTP_TLS.__new__(ImplicitFTP_TLS)
        client._sock = None
        already = mock.create_autospec(ssl.SSLSocket, instance=True)
        client.sock = already
        assert client.sock is already


# ---------------------------------------------------------------------------
# list_timelapses
# ---------------------------------------------------------------------------


class TestList:
    """Cover directory listing via MLSD and the NLST fallback."""

    def test_mlsd_parsing_and_filter(self, logger, patch_ftp):
        """MLSD entries are parsed and filtered to video files only."""
        fake = FakeFTP(
            mlsd_data=[
                (
                    "a.mp4",
                    {"size": "100", "modify": "20240101000000", "type": "file"},
                ),
                ("b.avi", {"size": "200", "type": "file"}),
                ("notes.txt", {"size": "5", "type": "file"}),
                ("sub", {"type": "dir"}),
            ]
        )
        patch_ftp(fake)
        with BambuTimelapseFtp(logger, "h", "c") as svc:
            files = svc.list_timelapses()
        names = {f["name"] for f in files}
        assert names == {"a.mp4", "b.avi"}
        a = next(f for f in files if f["name"] == "a.mp4")
        assert a["size"] == 100
        assert a["date"] == "20240101000000"

    def test_nlst_fallback(self, logger, patch_ftp):
        """When MLSD is unsupported, NLST names are used and sized."""
        fake = FakeFTP(
            mlsd_data=None,
            nlst_data=["a.mp4", "b.txt", "/timelapse/c.avi"],
            sizes={"a.mp4": 10, "c.avi": 20},
        )
        patch_ftp(fake)
        with BambuTimelapseFtp(logger, "h", "c") as svc:
            files = svc.list_timelapses()
        names = {f["name"] for f in files}
        assert names == {"a.mp4", "c.avi"}

    def test_remote_size(self, logger, patch_ftp):
        """remote_size returns the SIZE result or None when unavailable."""
        fake = FakeFTP(sizes={"a.mp4": 42})
        patch_ftp(fake)
        with BambuTimelapseFtp(logger, "h", "c") as svc:
            assert svc.remote_size("a.mp4") == 42
            assert svc.remote_size("missing.mp4") is None

    def test_remote_size_rejects_traversal(self, logger, patch_ftp):
        """remote_size rejects path-traversal names."""
        fake = FakeFTP()
        patch_ftp(fake)
        with BambuTimelapseFtp(logger, "h", "c") as svc:
            with pytest.raises(FtpError):
                svc.remote_size("../x")

    def test_missing_folder_is_empty(self, logger, patch_ftp):
        """A missing timelapse folder yields an empty listing."""
        fake = FakeFTP()

        def boom(path):  # pylint: disable=unused-argument
            raise ftplib.error_perm("550 no such dir")

        fake.cwd = boom
        patch_ftp(fake)
        with BambuTimelapseFtp(logger, "h", "c") as svc:
            assert not svc.list_timelapses()


# ---------------------------------------------------------------------------
# download
# ---------------------------------------------------------------------------


class TestDownload:
    """Cover streaming downloads, cleanup, and name validation."""

    def test_streams_then_renames(self, logger, patch_ftp, tmp_path):
        """Chunks stream to a .part file then rename into place."""
        fake = FakeFTP(
            retr_chunks={"v.mp4": [b"abc", b"def"]},
            sizes={"v.mp4": 6},
        )
        patch_ftp(fake)
        dest = str(tmp_path / "v.mp4")
        seen = []
        with BambuTimelapseFtp(logger, "h", "c") as svc:
            written = svc.download(
                "v.mp4", dest, progress_cb=lambda t, n: seen.append((t, n))
            )
        assert written == 6
        assert os.path.exists(dest)
        with open(dest, "rb") as fh:
            assert fh.read() == b"abcdef"
        assert not os.path.exists(dest + ".part")
        assert seen[-1] == (6, 6)

    def test_partial_cleanup_on_error(self, logger, patch_ftp, tmp_path):
        """A transfer error removes both the dest and the .part file."""
        fake = FakeFTP(sizes={"v.mp4": 6})

        def boom(cmd, callback):  # pylint: disable=unused-argument
            callback(b"abc")
            raise ftplib.error_temp("426 aborted")

        fake.retrbinary = boom
        patch_ftp(fake)
        dest = str(tmp_path / "v.mp4")
        with BambuTimelapseFtp(logger, "h", "c") as svc:
            with pytest.raises(ftplib.error_temp):
                svc.download("v.mp4", dest)
        assert not os.path.exists(dest)
        assert not os.path.exists(dest + ".part")

    def test_rejects_traversal_name(self, logger, patch_ftp, tmp_path):
        """download rejects traversal and path-separator names."""
        fake = FakeFTP()
        patch_ftp(fake)
        with BambuTimelapseFtp(logger, "h", "c") as svc:
            for bad in ("../etc/x", "a/b.mp4", "a\\b.mp4"):
                with pytest.raises(FtpError) as exc:
                    svc.download(bad, str(tmp_path / "x.mp4"))
                assert exc.value.reason == "bad_name"


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


class TestDelete:
    """Cover DELE issuing and name validation."""

    def test_delete_issues_dele(self, logger, patch_ftp):
        """delete forwards the name to the FTP DELE command."""
        fake = FakeFTP()
        patch_ftp(fake)
        with BambuTimelapseFtp(logger, "h", "c") as svc:
            svc.delete("v.mp4")
        assert fake.deleted == ["v.mp4"]

    def test_delete_rejects_traversal(self, logger, patch_ftp):
        """delete rejects path-traversal names without issuing DELE."""
        fake = FakeFTP()
        patch_ftp(fake)
        with BambuTimelapseFtp(logger, "h", "c") as svc:
            with pytest.raises(FtpError):
                svc.delete("../x")
        assert not fake.deleted


class TestThumbnail:
    """Cover thumbnail fetching, size limits, and 425 retry handling."""

    def test_fetch_thumbnail_bytes(self, logger, patch_ftp):
        """fetch_thumbnail returns the concatenated thumbnail bytes."""
        fake = FakeFTP(
            retr_chunks={
                "/timelapse/thumbnail/clip.jpg": [b"jpg", b"data"],
            }
        )
        patch_ftp(fake)
        with BambuTimelapseFtp(logger, "h", "c") as svc:
            data = svc.fetch_thumbnail("clip.avi")
        assert data == b"jpgdata"

    def test_missing_thumbnail_returns_none(self, logger, patch_ftp):
        """A missing thumbnail (550) returns None instead of raising."""
        fake = FakeFTP()  # no retr data → error_perm on RETR

        def boom(cmd, callback):  # pylint: disable=unused-argument
            raise ftplib.error_perm("550 not found")

        fake.retrbinary = boom
        patch_ftp(fake)
        with BambuTimelapseFtp(logger, "h", "c") as svc:
            assert svc.fetch_thumbnail("clip.avi") is None

    def test_thumbnail_rejects_traversal(self, logger, patch_ftp):
        """fetch_thumbnail rejects path-traversal names."""
        fake = FakeFTP()
        patch_ftp(fake)
        with BambuTimelapseFtp(logger, "h", "c") as svc:
            with pytest.raises(FtpError):
                svc.fetch_thumbnail("../x.avi")

    def test_oversized_thumbnail_returns_none(self, logger, patch_ftp):
        """A thumbnail over MAX_THUMBNAIL_BYTES is discarded (None)."""
        big = b"x" * (MAX_THUMBNAIL_BYTES + 1)
        fake = FakeFTP(retr_chunks={"/timelapse/thumbnail/clip.jpg": [big]})
        patch_ftp(fake)
        with BambuTimelapseFtp(logger, "h", "c") as svc:
            assert svc.fetch_thumbnail("clip.avi") is None

    def test_425_retries_then_succeeds(self, logger, patch_ftp):
        """A transient 425 is retried once on the same session."""
        fake = FakeFTP()
        calls = {"n": 0}

        def flaky(cmd, callback):  # pylint: disable=unused-argument
            calls["n"] += 1
            if calls["n"] == 1:
                raise ftplib.error_temp("425 Can't open data connection")
            callback(b"jpg")

        fake.retrbinary = flaky
        patch_ftp(fake)
        with BambuTimelapseFtp(logger, "h", "c") as svc:
            assert svc.fetch_thumbnail("clip.avi") == b"jpg"
        assert calls["n"] == 2

    def test_425_twice_returns_none(self, logger, patch_ftp):
        """Two 425s in a row give up gracefully (no crash)."""
        fake = FakeFTP()

        def always_425(cmd, callback):  # pylint: disable=unused-argument
            raise ftplib.error_temp("425")

        fake.retrbinary = always_425
        patch_ftp(fake)
        with BambuTimelapseFtp(logger, "h", "c") as svc:
            assert svc.fetch_thumbnail("clip.avi") is None


# ---------------------------------------------------------------------------
# connection reuse + error classification
# ---------------------------------------------------------------------------


class TestConnection:
    """Cover connection reuse and error-reason classification."""

    def test_batch_reuses_one_connection(self, logger, monkeypatch):
        """Repeated calls within one context share a single connection."""
        count = [0]
        fake = FakeFTP(
            mlsd_data=[("a.mp4", {"size": "1", "type": "file"})],
            retr_chunks={"a.mp4": [b"x"], "b.mp4": [b"y"]},
            sizes={"a.mp4": 1, "b.mp4": 1},
            connect_count=count,
        )

        def fake_connect(_self):
            fake.connect()
            return fake

        monkeypatch.setattr(BambuTimelapseFtp, "_connect", fake_connect)
        with BambuTimelapseFtp(logger, "h", "c") as svc:
            svc.list_timelapses()
            svc.list_timelapses()
        assert count[0] == 1

    def test_auth_failed_classification(self, logger, monkeypatch):
        """A 530 login failure is classified as auth_failed."""
        ftp = FakeFTP()
        ftp.login = lambda *_a: (_ for _ in ()).throw(
            ftplib.error_perm("530 Login incorrect")
        )
        monkeypatch.setattr(ftp_mod, "ImplicitFTP_TLS", lambda **_k: ftp)
        svc = BambuTimelapseFtp(logger, "h", "c")
        with pytest.raises(FtpError) as exc:
            svc.open()
        assert exc.value.reason == "auth_failed"

    def test_unreachable_classification(self, logger, monkeypatch):
        """A connection refusal is classified as unreachable."""
        ftp = FakeFTP()
        ftp.connect = lambda **_k: (_ for _ in ()).throw(OSError("refused"))
        monkeypatch.setattr(ftp_mod, "ImplicitFTP_TLS", lambda **_k: ftp)
        svc = BambuTimelapseFtp(logger, "h", "c")
        with pytest.raises(FtpError) as exc:
            svc.open()
        assert exc.value.reason == "unreachable"

    def test_timeout_classification(self, logger, monkeypatch):
        """A socket timeout is classified as timeout."""
        ftp = FakeFTP()
        ftp.connect = lambda **_k: (_ for _ in ()).throw(socket.timeout())
        monkeypatch.setattr(ftp_mod, "ImplicitFTP_TLS", lambda **_k: ftp)
        svc = BambuTimelapseFtp(logger, "h", "c")
        with pytest.raises(FtpError) as exc:
            svc.open()
        assert exc.value.reason == "timeout"

    def test_tls_error_classification(self, logger, monkeypatch):
        """An SSL error is classified as tls_error."""
        ftp = FakeFTP()
        ftp.connect = lambda **_k: (_ for _ in ()).throw(ssl.SSLError("bad"))
        monkeypatch.setattr(ftp_mod, "ImplicitFTP_TLS", lambda **_k: ftp)
        svc = BambuTimelapseFtp(logger, "h", "c")
        with pytest.raises(FtpError) as exc:
            svc.open()
        assert exc.value.reason == "tls_error"

    def test_close_best_effort(self, logger, patch_ftp):
        """close quits the session and is safe to call twice."""
        fake = FakeFTP()
        patch_ftp(fake)
        svc = BambuTimelapseFtp(logger, "h", "c")
        svc.open()
        svc.close()
        assert fake.quit_called
        # idempotent
        svc.close()
