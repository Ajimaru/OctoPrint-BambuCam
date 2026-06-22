"""FTPS client for downloading Bambu Lab printer timelapse videos.

The printer's SD card is served over **implicit FTPS on port 990** (user
``bblp`` / LAN access code). ``ImplicitFTP_TLS`` adds the two things stdlib
``ftplib`` lacks: implicit TLS (wrapped from byte 0) and data-connection TLS
session reuse (`bpo-25437`), without which every ``LIST``/``RETR`` aborts.
``BambuTimelapseFtp`` is the lockable, context-manager service the plugin uses.
"""

# nosec B402: used only over implicit TLS (FTPS) — the printer's only protocol
import ftplib  # nosec B402
import os
import socket
import ssl
import threading
from typing import Callable, Optional

VIDEO_EXTENSIONS = (".mp4", ".avi")
TIMELAPSE_DIR = "/timelapse"
THUMBNAIL_DIR = "/timelapse/thumbnail"
MAX_THUMBNAIL_BYTES = 4 * 1024 * 1024
CONNECT_TIMEOUT = 20


class FtpError(Exception):
    """An FTP operation failed, carrying a classified ``reason``.

    ``reason`` is one of ``unreachable`` / ``auth_failed`` / ``tls_error`` /
    ``timeout`` / ``network`` so the caller (and ultimately the UI) can show a
    friendly, translated message without parsing raw FTP strings.
    """

    def __init__(self, reason: str, message: str = ""):
        super().__init__(message or reason)
        self.reason = reason


class ImplicitFTP_TLS(
    ftplib.FTP_TLS
):  # noqa: N801  # pylint: disable=invalid-name  # mirror stdlib casing
    """``FTP_TLS`` variant for Bambu printers.

    Adds implicit TLS on the control socket and control->data TLS session
    reuse (vsftpd requires it).
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._sock: Optional[socket.socket] = None

    @property
    def sock(
        self,
    ) -> Optional[
        socket.socket
    ]:  # pyright: ignore[reportIncompatibleVariableOverride]
        """The control socket (typeshed types this as a plain attribute)."""
        return self._sock

    @sock.setter
    def sock(  # pyright: ignore[reportIncompatibleVariableOverride]
        self, value: Optional[socket.socket]
    ) -> None:
        """Wrap the control socket in TLS immediately (implicit FTPS)."""
        if value is not None and not isinstance(value, ssl.SSLSocket):
            value = self.context.wrap_socket(value)
        self._sock = value

    def ntransfercmd(self, cmd, rest=None):
        """Open a data connection, reusing the control TLS session."""
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:  # type: ignore[attr-defined]
            conn = self.context.wrap_socket(
                conn,
                server_hostname=self.host,
                session=self.sock.session,  # type: ignore
            )
        return conn, size


def _build_ssl_context() -> ssl.SSLContext:
    """Build the SSL context for the printer's self-signed cert.

    Mirrors the camera path (``vendor/webcamd_bambu/webcam.py``): TLS floored
    at 1.2, no cert/hostname verification (the printer has no stable hostname
    and presents a self-signed certificate).
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _reject_unsafe_name(name: str) -> None:
    """Reject a remote file name that could escape ``/timelapse``."""
    if not name or "/" in name or "\\" in name or ".." in name:
        raise FtpError("bad_name", f"unsafe remote name: {name!r}")


class BambuTimelapseFtp:
    """Lockable, context-managed FTPS service for the printer's timelapses.

    One instance manages **one** connection (the printer allows ~1 at a time).
    Open it as a context manager (or via ``open()`` / ``close()``) and call
    ``list_timelapses`` / ``download`` / ``delete`` on the open session.
    All operations are serialized by a per-instance lock.
    """

    def __init__(
        self,
        logger,
        hostname: str,
        access_code: str,
        *,
        timeout: int = CONNECT_TIMEOUT,
    ):
        self._logger = logger
        self._hostname = hostname
        self._access_code = access_code
        self._timeout = timeout
        self._ftp: Optional[ImplicitFTP_TLS] = None
        self._lock = threading.Lock()

    def __enter__(self) -> "BambuTimelapseFtp":
        self.open()
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def open(self) -> None:
        """Connect and log in. Idempotent within a batch."""
        if self._ftp is None:
            self._ftp = self._connect()

    def _connect(self) -> ImplicitFTP_TLS:
        """Build the TLS context, connect, log in, protect the data channel.

        Never logs the access code. Raises :class:`FtpError` with a classified
        ``reason`` on failure.
        """
        ctx = _build_ssl_context()
        ftp = ImplicitFTP_TLS(context=ctx)
        try:
            ftp.connect(host=self._hostname, port=990, timeout=self._timeout)
            ftp.login("bblp", self._access_code)
            ftp.prot_p()
        except ftplib.error_perm as exc:
            self._safe_close(ftp)
            raise FtpError("auth_failed", str(exc)) from exc
        except (socket.timeout, TimeoutError) as exc:
            self._safe_close(ftp)
            raise FtpError("timeout", str(exc)) from exc
        except ssl.SSLError as exc:
            self._safe_close(ftp)
            raise FtpError("tls_error", str(exc)) from exc
        except (OSError, *ftplib.all_errors) as exc:
            self._safe_close(ftp)
            raise FtpError("unreachable", str(exc)) from exc
        self._logger.debug("FTPS connected to printer")
        return ftp

    @staticmethod
    def _safe_close(ftp: ImplicitFTP_TLS) -> None:
        try:
            ftp.close()
        except OSError:
            pass

    def close(self) -> None:
        """Best-effort ``quit()`` then ``close()``."""
        if self._ftp is None:
            return
        try:
            self._ftp.quit()
        except (OSError, *ftplib.all_errors):
            self._safe_close(self._ftp)
        self._ftp = None

    def _reconnect(self) -> None:
        """Drop the current session and build a fresh one (one retry path)."""
        if self._ftp is not None:
            self._safe_close(self._ftp)
            self._ftp = None
        self._ftp = self._connect()

    @property
    def _conn(self) -> ImplicitFTP_TLS:
        if self._ftp is None:
            raise FtpError("network", "FTP session not open")
        return self._ftp

    def list_timelapses(self) -> list[dict]:
        """Return ``[{"name", "size", "date"}]`` for the timelapse videos.

        Tries ``MLSD`` (size + modify time) first, falls back to ``NLST`` +
        ``size()``. An empty or missing folder is a valid empty result, not an
        error. Filters to :data:`VIDEO_EXTENSIONS`.
        """
        with self._lock:
            ftp = self._conn
            try:
                ftp.cwd(TIMELAPSE_DIR)
            except ftplib.error_perm:
                return []
            try:
                return self._list_mlsd(ftp)
            except (ftplib.error_perm, ftplib.error_proto):
                return self._list_nlst(ftp)

    def _list_mlsd(self, ftp: ImplicitFTP_TLS) -> list[dict]:
        files = []
        for name, facts in ftp.mlsd():
            if not self._is_video(name):
                continue
            if facts.get("type", "file") == "dir":
                continue
            size = facts.get("size")
            files.append(
                {
                    "name": name,
                    "size": int(size) if size is not None else None,
                    "date": facts.get("modify"),
                }
            )
        return files

    def _list_nlst(self, ftp: ImplicitFTP_TLS) -> list[dict]:
        files = []
        for name in ftp.nlst():
            base = name.rsplit("/", 1)[-1]
            if not self._is_video(base):
                continue
            try:
                size = ftp.size(base)
            except (ftplib.error_perm, ftplib.error_proto):
                size = None
            files.append({"name": base, "size": size, "date": None})
        return files

    @staticmethod
    def _is_video(name: str) -> bool:
        return name.lower().endswith(VIDEO_EXTENSIONS)

    def remote_size(self, name: str) -> Optional[int]:
        """Return ``ftp.size(name)`` for the disk-space pre-check / progress."""
        _reject_unsafe_name(name)
        with self._lock:
            try:
                return self._conn.size(name)
            except (ftplib.error_perm, ftplib.error_proto):
                return None

    def download(
        self,
        name: str,
        dest_path: str,
        *,
        progress_cb: Optional[Callable[[int, Optional[int]], None]] = None,
    ) -> int:
        """Stream ``RETR <name>`` to ``dest_path`` atomically; return bytes.

        Writes to a temp file in the **same folder** as ``dest_path`` (so the
        final rename is an atomic same-device move), opened with
        ``O_EXCL|O_NOFOLLOW`` so a planted symlink can't redirect the write and
        an existing file is never clobbered. On any failure the partial temp
        file is removed. The caller is responsible for building a safe,
        contained ``dest_path`` (see ``__init__.py`` / plan §5.7).

        ``progress_cb(transferred, total)`` is invoked from the chunk callback.
        """
        _reject_unsafe_name(name)
        folder = os.path.dirname(dest_path)
        tmp_path = dest_path + ".part"
        total = self.remote_size(name)

        flags = (
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        )
        written = 0
        with self._lock:
            ftp = self._conn
            try:
                fd = os.open(tmp_path, flags, 0o644)
                with os.fdopen(fd, "wb") as fh:

                    def _chunk(data: bytes) -> None:
                        nonlocal written
                        fh.write(data)
                        written += len(data)
                        if progress_cb is not None:
                            progress_cb(written, total)

                    ftp.retrbinary(f"RETR {name}", _chunk)
                os.replace(tmp_path, dest_path)
            except Exception:
                self._cleanup(tmp_path)
                raise
        self._logger.debug(
            "downloaded %s (%d bytes) to %s", name, written, folder
        )
        return written

    @staticmethod
    def _cleanup(path: str) -> None:
        try:
            os.remove(path)
        except OSError:
            pass

    def delete(self, name: str) -> None:
        """Issue ``DELE <name>`` on the open session.

        Rejects unsafe names first. Raises on FTP error so the caller can
        classify/report. The caller (not this method) enforces the §5.9 guards
        (print-active, confirmation, copy-verified-before-delete).
        """
        _reject_unsafe_name(name)
        with self._lock:
            self._conn.delete(name)
        self._logger.debug("deleted remote %s", name)

    def fetch_thumbnail(self, name: str) -> Optional[bytes]:
        """Return the JPEG preview bytes for video ``name``, or ``None``.

        The printer stores ``/timelapse/thumbnail/<videostem>.jpg``. Streams it
        into memory (capped at :data:`MAX_THUMBNAIL_BYTES`). A missing thumbnail
        is a normal ``None`` result, not an error. Rejects unsafe names first.
        """
        _reject_unsafe_name(name)
        stem = name.rsplit(".", 1)[0]
        remote = f"{THUMBNAIL_DIR}/{stem}.jpg"
        buf = bytearray()
        with self._lock:
            ftp = self._conn

            def _chunk(data: bytes) -> None:
                if len(buf) + len(data) > MAX_THUMBNAIL_BYTES:
                    raise FtpError("too_large", "thumbnail exceeds cap")
                buf.extend(data)

            try:
                ftp.retrbinary(f"RETR {remote}", _chunk)
            except ftplib.error_temp:
                buf.clear()
                try:
                    ftp.retrbinary(f"RETR {remote}", _chunk)
                except ftplib.all_errors:
                    return None
            except (ftplib.error_perm, ftplib.error_proto):
                return None
            except FtpError:
                return None
        return bytes(buf)
