# Data flow

Three distinct paths move data between the browser, the plugin, the daemon and
the printer.

## 1. Live stream (browser → daemon)

The webcam tab renders an MJPEG `<img>` whose `src` is computed client-side:

```text
browser <img>  ──►  http://<host>:<port>/?stream  ──►  webcam.py  ──►  printer cam
```

- `stream_url_override` wins if set (reverse-proxy setups).
- Otherwise the frontend rewrites the loopback host to the **browser's current
  host** (`location.hostname`) — so live view only works when the daemon binds
  `0.0.0.0`, or when the browser runs on the OctoPrint host.

## 2. Snapshot & timelapse (plugin → daemon, loopback only)

```text
OctoPrint timelapse ─► take_webcam_snapshot ─► http://127.0.0.1:<port>/?snapshot
```

Snapshots **always** use the `127.0.0.1` loopback URL regardless of the bind
address, so they work even with the safe `bind_address = 127.0.0.1` default.

## 3. Control & status (settings dialog → plugin)

The frontend uses OctoPrint's simple-API:

| Action          | Call                                       | Permission |
| --------------- | ------------------------------------------ | ---------- |
| Poll status     | `simpleApiGet("bambucam")`                 | SETTINGS   |
| Restart daemon  | `simpleApiCommand(..., "restart")`         | ADMIN      |
| Test connection | `simpleApiCommand(..., "test_connection")` | ADMIN      |
| Fetch `/?info`  | `simpleApiCommand(..., "fetch_info")`      | SETTINGS   |

Status is polled every 10 s while the settings dialog is open. The plugin also
pushes `daemon_state` messages over the data updater for crash/recovery events.

See the [HTTP API reference](../reference/http-api.md) for request/response
shapes.
