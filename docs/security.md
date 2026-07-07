# Security

The plugin's threat model centers on the printer **access code**, the
unauthenticated MJPEG server, and the privileged actions that read or write the
printer (SD-card transfers over FTPS, light control over MQTT).

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

All three printer connections floor the protocol at **TLS 1.2**
(`ssl.TLSVersion.TLSv1_2`) so legacy TLS 1.0/1.1 are never negotiated:

- the **camera** port (6000) — `daemon.py` (connection test) and the vendored
  `webcam.py` (stream, local patch #5);
- **FTPS** (990) for SD-card timelapses — `ftp.py`;
- **MQTT** (8883) for light control — `mqtt.py`, used only as a fallback.

Certificate and hostname verification stay disabled on all of them because Bambu
printers present a self-signed certificate and have no stable hostname; this is
an inherent constraint of the LAN protocol, not a configurable option.

Light control normally goes through **OctoPrint-BambuConnector's existing
connection** (`connector_led.py`), not our own MQTT — the printer's broker
allows very few sockets, so we reuse the one BambuConnector already holds rather
than opening a second. That path inherits BambuConnector's own TLS posture.

## Privileged actions

The SD-card and light operations are permission-gated and the access code is
**never logged**:

- **Reads** (`list_timelapses`, `fetch_info`, `detect_connector`,
  `ffmpeg_status`) require **SETTINGS**.
- **SD-card writes** (`copy`/`move`/`delete`, `convert_local_avi`) require
  **ADMIN**, and `move`/`delete` are blocked while a print is running. A `move`
  deletes the SD original only after the local copy is verified byte-for-byte;
  downloads are size-checked against free disk space first.
- The **light toggle** (`set_led`) requires **CONTROL**.

## Reporting

Report vulnerabilities by email to **ajimaru_gdr [at] pm [dot] me** — please do
**not** open a public issue. See the full
[Security Policy](https://github.com/Ajimaru/OctoPrint-BambuCam/blob/main/SECURITY.md)
for scope and response times.
