# Security

The plugin's threat model centers on the printer **access code** and the
unauthenticated MJPEG server.

## Access code handling

- Stored in OctoPrint settings as an **admin-restricted** path
  (`get_settings_restricted_paths`), so non-admin clients never receive it.
- Passed to the daemon on its command line, so it **is** visible in the local
  process list on the OctoPrint host.
- The vendored daemon is patched so the access code is **never returned** by its
  `/?info` endpoint. `WebcamdManager.fetch_info()` strips `config.password`
  again as defense-in-depth, in case the vendor patch is lost in an upstream
  update.

## Loopback-only sensitive paths

Snapshots, timelapse capture and `/?info` always connect via
`http://127.0.0.1:<port>` regardless of the configured bind address.

## Bind address trade-off

The bundled MJPEG server has **no authentication**.

!!! warning
With `bind_address = 0.0.0.0`, anyone on your network can watch the camera
stream. Keep the `127.0.0.1` default unless you need browser live view and
have a reverse proxy with authentication in front.

This is a **documented trade-off**, not a vulnerability — see the
[Security Policy](https://github.com/Ajimaru/OctoPrint-BambuCam/blob/main/SECURITY.md).

## Disabled upstream endpoint

The vendored daemon's unauthenticated `/?shutdown` endpoint is **disabled** (see
`octoprint_bambucam/vendor/webcamd_bambu/UPSTREAM.md`) so it cannot be used to
kill the stream server remotely.

## TLS to the printer

The connection to the printer's camera port (6000) floors the protocol at
**TLS 1.2** (`ssl.TLSVersion.TLSv1_2`) so the legacy TLS 1.0/1.1 versions are
never negotiated — both in `daemon.py` (the connection test) and in the vendored
`webcam.py` (the stream, local patch #5). Certificate and hostname verification
stay disabled because Bambu printers present a self-signed certificate and use a
custom binary handshake; this is an inherent constraint of the LAN protocol, not
a configurable option.

## Reporting

Report vulnerabilities by email to **ajimaru_gdr [at] pm [dot] me** — please do
**not** open a public issue. See the full
[Security Policy](https://github.com/Ajimaru/OctoPrint-BambuCam/blob/main/SECURITY.md)
for scope and response times.
