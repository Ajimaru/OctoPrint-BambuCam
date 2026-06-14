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

## Reporting

Report vulnerabilities by email to **ajimaru_gdr@pm.me** — please do **not**
open a public issue. See the full
[Security Policy](https://github.com/Ajimaru/OctoPrint-BambuCam/blob/main/SECURITY.md)
for scope and response times.
