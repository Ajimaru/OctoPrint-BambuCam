# Architecture overview

BambuCam has three layers: the **OctoPrint plugin** (Python), a supervised
**vendored daemon** (the MJPEG server), and a **frontend viewmodel**
(JavaScript) bound into the settings dialog and webcam tab.

## Components

| Layer             | File                                                | Role                                                        |
| ----------------- | --------------------------------------------------- | ----------------------------------------------------------- |
| Plugin            | `octoprint_bambucam/__init__.py`                    | Wires OctoPrint mixins, exposes the webcam + simple API.    |
| Daemon supervisor | `octoprint_bambucam/daemon.py`                      | Spawns/monitors/restarts the MJPEG child process.           |
| Vendored daemon   | `octoprint_bambucam/vendor/webcamd_bambu/webcam.py` | MJPEG HTTP server talking to the printer camera (TCP 6000). |
| Frontend          | `octoprint_bambucam/static/js/BambuCam.js`          | Settings status panel, connection test, stream `<img>`.     |

## Mixins used

`BambucamPlugin` combines seven OctoPrint mixins:

- `StartupPlugin` / `ShutdownPlugin` — start and stop the daemon with OctoPrint.
- `SettingsPlugin` — defaults, restricted paths, restart-on-change.
- `TemplatePlugin` — settings + webcam Jinja2 templates.
- `AssetPlugin` — bundles `BambuCam.js` and `BambuCam.css`.
- `SimpleApiPlugin` — `restart`, `test_connection`, `fetch_info` commands.
- `WebcamProviderPlugin` — registers the webcam and serves snapshots.

## High-level flow

```text
OctoPrint boot
   └─ on_after_startup ──► WebcamdManager.start(config)
                               └─ subprocess: webcam.py  (MJPEG @ :8181)
                                      ▲
browser webcam tab ── MJPEG <img> ────┘
settings dialog ── simpleApi (restart / test / info) ─► plugin ─► WebcamdManager
```

See [Daemon supervisor](daemon.md) for the process lifecycle and
[Data flow](data-flow.md) for the request paths.
