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
always stripped.

## `POST /api/plugin/bambucam`

Body is `{"command": "...", ...}`.

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

## Push messages

The plugin pushes `daemon_state` events over OctoPrint's data updater:

```json
{ "type": "daemon_state", "state": "gave_up", "detail": { "error": "..." } }
```

`state` is one of `started`, `stopped`, `crashed`, `gave_up`.
