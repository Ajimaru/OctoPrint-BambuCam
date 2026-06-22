# HTTP API reference

The plugin exposes an OctoPrint **simple API** under
`/api/plugin/bambucam`. All endpoints are protected (`is_api_protected()` →
`True`) and require an OctoPrint API key plus the permission noted below.

## `GET /api/plugin/bambucam`

Returns the daemon status snapshot. Requires **SETTINGS**.

```json
{
  "running": true,
  "pid": 12345,
  "uptime": 412.7,
  "restarts": 0,
  "last_error": null,
  "info": { "stats": { "encodeFps": 1.8, "sessionCount": 1 } },
  "stream_url": "http://127.0.0.1:8181/?stream"
}
```

`info` is `null` when the daemon is not running, and its `config.password` is
always stripped. The response also carries `led_available` (boolean) — `true`
only when the printer serial is known (supplied by OctoPrint-BambuConnector),
which gates the live-stream light toggle — and `led_on` (`true` / `false` /
`null`), the real chamber-light state. This is read directly from
BambuConnector when it is the active connection, else from the fallback MQTT
monitor; `null` means unknown.

## `POST /api/plugin/bambucam`

Body is `{"command": "...", ...}`. Permission required per command:

| Command             | Permission   |
| ------------------- | ------------ |
| `detect_connector`  | **SETTINGS** |
| `ffmpeg_status`     | **SETTINGS** |
| `fetch_info`        | **SETTINGS** |
| `list_timelapses`   | **SETTINGS** |
| `list_local_avi`    | **SETTINGS** |
| `led_monitor_start` | **SETTINGS** |
| `led_monitor_stop`  | **SETTINGS** |
| `set_led`           | **CONTROL**  |
| `restart`           | **ADMIN**    |
| `test_connection`   | **ADMIN**    |
| `copy_timelapses`   | **ADMIN**    |
| `move_timelapses`   | **ADMIN**    |
| `delete_timelapses` | **ADMIN**    |
| `convert_local_avi` | **ADMIN**    |

### `restart`

Requires **ADMIN**. Restarts the daemon with the current config.

```json
{ "command": "restart" }
```

Response: `{ "ok": true, "error": null }`.

### `test_connection`

Requires **ADMIN**. Probes the printer camera handshake without starting the
daemon. The request is capped at ~12 s.

```json
{
  "command": "test_connection",
  "hostname": "192.168.1.100",
  "access_code": "12345678"
}
```

Response: `{ "ok": true, "reason": "ok" }`. On failure `reason` is one of
`unreachable`, `auth_failed`, `timeout`, `error`.

### `fetch_info`

Requires **SETTINGS**. Reads the daemon's `/?info` (access code redacted).

```json
{ "command": "fetch_info" }
```

Response: `{ "ok": true, "info": { ... } }`, or
`{ "ok": false, "reason": "unreachable" }`.

### `detect_connector`

Requires **SETTINGS**. Probes OctoPrint-BambuConnector's connection profile for
a reusable host / access code / serial. Reads config only, never hits the
network.

```json
{ "command": "detect_connector" }
```

Response: `{ "ok": true, "connector": { "installed": true, "available": true,
"hostname": "192.168.1.100", "serial": "0300...", "has_access_code": true } }`.
The access code itself is never returned — only `has_access_code`.

### `ffmpeg_status`

Requires **SETTINGS**. Reports whether OctoPrint's ffmpeg (used for the
`.avi` → `.mp4` transcode) is configured and runnable, for the settings
indicator.

```json
{ "command": "ffmpeg_status" }
```

Response: `{ "ok": true, "ffmpeg": { "path": "/usr/bin/ffmpeg",
"configured": true, "executable": true } }`.

### `set_led`

Requires **CONTROL**. Toggles the printer light. Needs the printer serial (from
Bambu Connector); the request is capped at ~20 s.

```json
{ "command": "set_led", "on": true }
```

Response: `{ "ok": true }`, or `{ "ok": false, "reason": "..." }` where
`reason` is one of `busy`, `no_serial`, `unreachable`, `auth_failed`,
`timeout`, `publish_failed`, `error`. Only one light command runs at a time;
a second one while the first is in flight returns `busy`.

The command is sent, **when possible, over BambuConnector's already-open
connection** (its `bpm.BambuPrinter.light_state`) — the printer's MQTT broker
allows very few sockets and BambuConnector holds one, so a second connection of
our own would time out. Only when BambuConnector is not the active connection
does it fall back to our own MQTT client (TCP `8883`), reusing the monitor's
standing socket if one is open.

### `led_monitor_start` / `led_monitor_stop`

Requires **SETTINGS**. Opens or closes a standing MQTT connection that tracks
the printer's real chamber-light state, used **only as a fallback** when
BambuConnector is not the active connection (otherwise the state is read
directly from BambuConnector and no monitor is opened). The frontend starts it
when the webcam (Control) tab is shown and stops it on leave, so the connection
only exists while needed.

```json
{ "command": "led_monitor_start" }
```

`start` response: `{ "ok": true, "led_on": true|false|null }` (the last known
state, `null` until the printer reports), or
`{ "ok": false, "reason": "no_serial" | "unreachable" | "auth_failed" |
"timeout" }`. `stop` always returns `{ "ok": true }`. While the monitor is open,
light-state changes are pushed as `led_state` messages (see below).

### `list_timelapses`

Requires **SETTINGS**. Lists the printer's SD-card timelapses over FTPS
(TCP `990`). Capped at ~30 s.

```json
{ "command": "list_timelapses" }
```

Response: `{ "ok": true, "files": [ { "name": "video_...avi", "size": 12345,
"date": "...", "date_corrected": true, "copied": true,
"renamed": "video_....mp4" } ] }`, or
`{ "ok": false, "reason": "..." }` (`unreachable`, `auth_failed`, `tls_error`,
`timeout`, `error`). `copied` marks files already pulled locally; `renamed` is
present only when the local file differs (e.g. a transcoded `.mp4`).

The printer's raw SD-card date is unreliable on some firmware (see the date
note in [Configuration](configuration.md)), so each file carries exactly one of:

- **`date_corrected: true`** — `date` is a real, trustworthy time: a copied
  file's local mtime, or the print-end time recorded for this video name.
- **`date_unreliable: true`** — no trustworthy date is known; the UI shows
  "Date unknown" rather than the bogus camera-clock date.

### `copy_timelapses` / `move_timelapses` / `delete_timelapses`

Requires **ADMIN**. Runs a batch transfer over the printer's single FTPS
connection. `copy` keeps the SD original; `move` deletes it only after a
byte-for-byte verify; `delete` removes it. Returns immediately and reports
progress over the `timelapse_op` push channel.

```json
{ "command": "copy_timelapses", "names": ["video_2026-06-21_10-00-00.avi"] }
```

Response: `{ "ok": true }`, or `{ "ok": false, "reason": "..." }`
(`busy`, `printing`, `bad_name`). `move`/`delete` are rejected while a print is
running.

### `list_local_avi`

Requires **SETTINGS**. Lists already-downloaded `.avi` files awaiting
conversion.

```json
{ "command": "list_local_avi" }
```

Response: `{ "ok": true, "files": [ { "name": "...avi", "size": 123 } ] }`.

### `convert_local_avi`

Requires **ADMIN**. Transcodes the named local `.avi` files to `.mp4`. Blocked
while a print is running and when ffmpeg is unavailable. Progress is reported
over the `convert_op` push channel.

```json
{ "command": "convert_local_avi", "names": ["video_....avi"] }
```

Response: `{ "ok": true }`, or `{ "ok": false, "reason": "..." }`
(`printing`, `no_ffmpeg`, `bad_name`, `busy`).

## Push messages

The plugin pushes `daemon_state` events over OctoPrint's data updater:

```json
{ "type": "daemon_state", "state": "gave_up", "detail": { "error": "..." } }
```

`state` is one of `started`, `stopped`, `crashed`, `offline`, `gave_up`.

The `offline` state is pushed when the printer is unreachable (powered off);
the daemon reconnects automatically, so it is informational rather than an
error. Its `detail` carries the child `returncode` (`75`). See
[Printer-offline handling](../architecture/daemon.md#printer-offline-handling).

The timelapse subsystem pushes its own progress events so the tab can update
live without polling:

- **`timelapse_op`** — a copy/move/delete batch. Per-file messages carry
  `name`, `state` (`progress`, `converting`, `done`), `percent` and the
  `transferred` / `total` byte counters (for the ETA); the final message has
  `state: "batch_done"` and a `summary` (e.g. `{ "copied": 2, "skipped": 1 }`).
- **`convert_op`** — the same shape for a local `.avi` → `.mp4` conversion
  batch.
- **`auto_sync`** — emitted when an after-print auto-sync runs:
  `{ "type": "auto_sync", "state": "started", "count": 1, "action": "copy" }`.
- **`led_state`** — the real chamber-light state, pushed by the fallback MQTT
  monitor whenever it changes: `{ "type": "led_state", "on": true }`. (When
  BambuConnector drives the light, the UI reads the state from the GET status
  instead.)
