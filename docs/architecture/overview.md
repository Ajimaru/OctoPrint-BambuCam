# Architecture overview

BambuCam has two functional halves. The **live stream** runs through a
supervised **vendored daemon** (the MJPEG server). The **timelapse and printer
controls** talk to the printer directly from the plugin over FTPS and MQTT. A
single **frontend viewmodel** (JavaScript) drives the settings dialog, webcam
tab and timelapse tab.

## Components

| Layer             | File                                                | Role                                                                |
| ----------------- | --------------------------------------------------- | ------------------------------------------------------------------- |
| Plugin            | `octoprint_bambucam/__init__.py`                    | Wires OctoPrint mixins, exposes the webcam + simple API.            |
| Daemon supervisor | `octoprint_bambucam/daemon.py`                      | Spawns/monitors/restarts the MJPEG child process.                   |
| Vendored daemon   | `octoprint_bambucam/vendor/webcamd_bambu/webcam.py` | MJPEG HTTP server talking to the printer camera (TCP 6000).         |
| Timelapse ops     | `octoprint_bambucam/timelapse_ops.py`               | Copy/move/delete batches + `.avi` → `.mp4` transcode (mixin).       |
| Auto-sync         | `octoprint_bambucam/autosync.py`                    | Pulls the new timelapse after a print, once the system is idle.     |
| FTPS client       | `octoprint_bambucam/ftp.py`                         | Implicit-TLS FTPS to the printer's SD card (TCP 990).               |
| Connector light   | `octoprint_bambucam/connector_led.py`               | Drives the light over BambuConnector's open connection (preferred). |
| MQTT client       | `octoprint_bambucam/mqtt.py`                        | Fallback light control over the printer's MQTT broker (TCP 8883).   |
| Transcoder        | `octoprint_bambucam/transcode.py`                   | Wraps OctoPrint's ffmpeg for the `.avi` → `.mp4` re-encode.         |
| Connector probe   | `octoprint_bambucam/bambu_connector.py`             | Best-effort auto-config from OctoPrint-BambuConnector.              |
| Frontend          | `octoprint_bambucam/static/js/BambuCam.js`          | Settings panel, connection test, stream `<img>`, timelapse tab.     |

## Mixins used

`BambucamPlugin` combines OctoPrint mixins with two of its own
(`TimelapseOpsMixin`, `AutoSyncMixin`):

- `StartupPlugin` / `ShutdownPlugin` — start and stop the daemon with OctoPrint.
- `SettingsPlugin` — defaults, restricted paths, restart-on-change.
- `TemplatePlugin` — settings, webcam and timelapse Jinja2 templates.
- `AssetPlugin` — bundles `BambuCam.js` and `BambuCam.css`.
- `SimpleApiPlugin` — all the GET/POST commands (see the
  [HTTP API](../reference/http-api.md)).
- `EventHandlerPlugin` — drives auto-sync off OctoPrint's print/render events.
- `WebcamProviderPlugin` — registers the webcam and serves snapshots.

## High-level flow

```text
OctoPrint boot
   └─ on_after_startup ──► WebcamdManager.start(config)
                               └─ subprocess: webcam.py  (MJPEG @ :8181)
                                      ▲
browser webcam tab ── MJPEG <img> ────┘
settings/webcam ── simpleApi (restart / test / info / set_led) ─► plugin
timelapse tab ── simpleApi (list / copy / move / convert) ─► FTPS + ffmpeg
print ends ──► EventHandler ─► auto-sync (when idle) ─► FTPS + ffmpeg
```

See [Daemon supervisor](daemon.md) for the process lifecycle and
[Data flow](data-flow.md) for the request paths.
