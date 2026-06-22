# Data flow

Several distinct paths move data between the browser, the plugin, the daemon
and the printer. The live stream goes through the daemon; everything else
(timelapses, light) the plugin handles directly.

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

## 3. Timelapse management (plugin → printer SD card, FTPS)

```text
timelapse tab ─► simpleApi (list/copy/move) ─► ftp.py ─► SD card (TCP 990, TLS)
                                                  └─► transcode.py (ffmpeg → .mp4)
```

Operations are serialized over the printer's **single** FTPS connection
(thumbnails too). Copy keeps the SD original; move deletes it only after a
byte-for-byte verify; both run on a background thread and stream progress to the
tab via `timelapse_op` push messages. After a print, `autosync.py` runs this
same pipeline automatically once the printer and OctoPrint are both idle.

## 4. Light control & state (plugin → printer)

```text
light button ─► set_led ─┬─(preferred)─► connector_led.py ─► BambuConnector's
                         │                  open connection (bpm.BambuPrinter.light_state)
                         └─(fallback)──► mqtt.py ─► broker (TCP 8883, TLS)
```

The printer's MQTT broker tolerates only ~1-2 connections and **BambuConnector
already holds one**, so a second connection of our own times out intermittently.
The light is therefore driven, **when possible, over BambuConnector's existing
connection** (`connector_led.py`): it reaches the active OctoPrint 2.0
connection's `bpm.BambuPrinter` and sets its `light_state` property — the setter
publishes the toggle and the getter reports the real state, all on the
already-open socket. No extra connection, no slot contention.

Only when BambuConnector is **not** the active connection does `set_led` fall
back to our own `mqtt.py` client (`BambuMqttClient` one-shot, or the
`BambuMqttMonitor` standing connection while the webcam tab is open, which also
pushes `led_state` updates). The serial comes from OctoPrint-BambuConnector;
without it the button is hidden.

## 5. Control & status (settings dialog → plugin)

The frontend uses OctoPrint's simple-API:

| Action           | Call                                        | Permission |
| ---------------- | ------------------------------------------- | ---------- |
| Poll status      | `simpleApiGet("bambucam")`                  | SETTINGS   |
| Restart daemon   | `simpleApiCommand(..., "restart")`          | ADMIN      |
| Test connection  | `simpleApiCommand(..., "test_connection")`  | ADMIN      |
| Fetch `/?info`   | `simpleApiCommand(..., "fetch_info")`       | SETTINGS   |
| Detect connector | `simpleApiCommand(..., "detect_connector")` | SETTINGS   |
| ffmpeg status    | `simpleApiCommand(..., "ffmpeg_status")`    | SETTINGS   |
| Toggle light     | `simpleApiCommand(..., "set_led")`          | CONTROL    |

Status is polled every 10 s while the settings dialog is open. The plugin also
pushes `daemon_state`, `timelapse_op`, `convert_op` and `auto_sync` messages
over the data updater.

See the [HTTP API reference](../reference/http-api.md) for request/response
shapes.
