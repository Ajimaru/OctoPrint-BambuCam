# OctoPrint-BambuCam

Live MJPEG camera stream from Bambu Lab printers — inside OctoPrint.

[![License](https://img.shields.io/github/license/Ajimaru/OctoPrint-BambuCam)](https://github.com/Ajimaru/OctoPrint-BambuCam/blob/main/LICENSE)
[![OctoPrint](https://img.shields.io/badge/OctoPrint-1.10.0%2B-blue.svg)](https://octoprint.org)
[![Python](https://img.shields.io/badge/python-3.7%2B-blue.svg)](https://python.org)

!!! note "What this site is"
This is the **developer and API documentation** for the plugin. For
installation and end-user instructions, see the
[project README](https://github.com/Ajimaru/OctoPrint-BambuCam#readme).

## What it does

BambuCam bundles and supervises a small MJPEG HTTP server
([webcamd-bambu](https://github.com/disconn3ct/webcamd-bambu), `bambu` branch)
that connects to a Bambu Lab printer's camera port (TCP `6000`, TLS) using the
LAN access code, and exposes it to OctoPrint through the standard webcam,
settings, template, asset and simple-API plugin mixins.

## Highlights

- **Live MJPEG stream** of P1P / P1S / A1 / A1 mini cameras in OctoPrint.
- **Auto-managed daemon** — starts after boot, restarts on crash with
  exponential backoff, reconfigures on settings change.
- **Snapshot & timelapse** via the `WebcamProviderPlugin` mixin.
- **Connection test** of IP + access code before saving.
- **Security hardened** — access code never exposed via the daemon's
  `/?info`; unauthenticated `/?shutdown` disabled; loopback-only snapshots.

## Supported printers

| Series       | Supported | Notes                                     |
| ------------ | :-------: | ----------------------------------------- |
| P1P / P1S    |    ✅     | LAN mode, access code required            |
| A1 / A1 mini |    ✅     | LAN mode, access code required            |
| X1 / X1C     |    ❌     | Uses RTSPS (port 322), different protocol |

## Documentation map

- **[Getting started](getting-started.md)** — clone, install, run the dev stack.
- **[Architecture](architecture/overview.md)** — how the plugin and daemon fit
  together.
- **[API](api/python.md)** — auto-generated Python and JavaScript references.
- **[Frontend](frontend/ui-placements.md)** — UI placements and i18n.
- **[Security](security.md)** — threat model and hardening.
- **[Development](development/contributing.md)** — contributing, testing,
  building these docs, releases.
- **[Reference](reference/configuration.md)** — settings keys and the HTTP API.
