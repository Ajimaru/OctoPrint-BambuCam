---
layout: plugin

id: BambuCam
title: BambuCam
description: Bambu Lab camera stream integration for OctoPrint
authors:
  - Ajimaru
license: AGPL-3.0-or-later

date: 2026-06-13

homepage: https://github.com/Ajimaru/OctoPrint-BambuCam
source: https://github.com/Ajimaru/OctoPrint-BambuCam
archive: https://github.com/Ajimaru/OctoPrint-BambuCam/archive/main.zip

tags:
  - bambu
  - bambu lab
  - camera
  - webcam
  - mjpeg
  - stream
  - timelapse
  - p1p
  - p1s
  - a1

screenshots:
  - url: /assets/img/plugins/BambuCam/stream.png
    alt: Live camera stream in the OctoPrint Control tab
    caption: Live MJPEG stream from a Bambu Lab printer

featuredimage: /assets/img/plugins/BambuCam/stream.png

# Compatibility

compatibility:
  # OctoPrint 1.10.0 and up (matches the CI smoke matrix and dependency).
  octoprint:
    - 1.10.0

  # The bundled daemon and OctoPrint run on POSIX systems.
  os:
    - linux
    - macos
    - freebsd

  # Tested on Python 3.9 through 3.13 (see CI matrix).
  python: ">=3.9,<4"

attributes:
---

Live camera stream from Bambu Lab printers — right inside OctoPrint's Control
tab.

## Highlights

- **Live MJPEG Stream** — Camera of P1P / P1S / A1 / A1 mini in OctoPrint's
  Control tab
- **Auto-managed Daemon** — Starts, restarts on crash, and reconfigures without
  any manual setup
- **Snapshot & Timelapse** — Full `WebcamProviderPlugin` integration for
  OctoPrint's built-in timelapse engine
- **Connection Test** — Verify IP and access code before saving settings
- **Rotation & Overlays** — Image rotation, FPS watermark, and activity dot
- **Security Hardened** — Access code never exposed via daemon HTTP;
  unauthenticated shutdown endpoint disabled
- **Reverse-Proxy Ready** — Stream URL override for setups behind nginx / Caddy
- **Bind Address Control** — `127.0.0.1` (safe default) or `0.0.0.0` for browser
  live view

## Supported printers

| Series       | Supported | Notes                           |
| ------------ | :-------: | ------------------------------- |
| P1P / P1S    |    yes    | LAN mode, access code required  |
| A1 / A1 mini |    yes    | LAN mode, access code required  |
| X1 / X1C     |    no     | RTSPS (port 322) — out of scope |

The printer camera delivers roughly **0.5–2 FPS** by design — a limitation of
the printer firmware, not the plugin. If Bambu Studio or the Bambu Handy app is
watching the camera simultaneously, the connection may fail.

## Configuration

Open **Settings → Plugins → BambuCam** and fill in the printer IP / hostname
and the access code (shown on the printer display under _Network_ settings).
Use **Test connection** to verify both values before saving.

The bundled MJPEG server has **no authentication**. With `bind_address =
0.0.0.0`, anyone on your network can watch the camera stream. Keep `127.0.0.1`
(the default) unless you need browser live view behind a reverse proxy.

See the [project README][readme] for the full list of optional settings,
security notes, and troubleshooting steps.

[readme]: https://github.com/Ajimaru/OctoPrint-BambuCam#readme
