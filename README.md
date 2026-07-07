<!-- markdownlint-disable MD041 MD033 -->
<p align="center">
  <img
    src="assets/img/bambucam.svg"
    alt="OctoPrint BambuCam Logo"
    width="64"
    height="64"
  />
</p>
<h1 align="center">OctoPrint‑BambuCam</h1>
<!-- markdownlint-enable MD041 MD033 -->

[![License][badge-license]](https://github.com/Ajimaru/OctoPrint-BambuCam/blob/main/LICENSE)
[![Python][badge-python]](https://python.org)
[![OctoPrint][badge-octoprint]](https://octoprint.org)
[![Latest Release][badge-release]](https://github.com/Ajimaru/OctoPrint-BambuCam/releases/latest)
[![Downloads][badge-downloads]](https://github.com/Ajimaru/OctoPrint-BambuCam/releases)
[![Made with Love][badge-love]](https://github.com/Ajimaru/OctoPrint-BambuCam)

[badge-license]: https://img.shields.io/github/license/Ajimaru/OctoPrint-BambuCam?style=flat-square
[badge-python]: https://img.shields.io/badge/python-3.9%2B-blue.svg?style=flat-square
[badge-octoprint]: https://img.shields.io/badge/OctoPrint-1.10.0%2B-blue.svg?style=flat-square
[badge-release]: https://img.shields.io/github/v/release/Ajimaru/OctoPrint-BambuCam?style=flat-square&sort=semver
[badge-downloads]: https://img.shields.io/github/downloads/Ajimaru/OctoPrint-BambuCam/total.svg?style=flat-square
[badge-love]: https://img.shields.io/badge/made_with-%E2%9D%A4%EF%B8%8F-ff69b4?style=flat-square

### Live camera stream from Bambu Lab printers — inside OctoPrint

[![71% Vibe_Coded](https://img.shields.io/badge/71%25-Vibe_Coded-ff69b4?style=flat-square&logo=claude&logoColor=white)](https://github.com/ai-ecoverse/vibe-coded-badge-action)

> [!NOTE]
> **About this project.** I built this for my own printer setup with AI, and if
> it helps others, even better. I have tested it to the best of my knowledge and
> ability, and every change is backed by an automated test suite, CI, and
> security scans (Bandit, CodeQL). Disclosed here per the OctoPrint plugin guidelines.
> Issues and PRs are welcome.

## Highlights

- 📷 **Live MJPEG Stream** — Camera of P1P / P1S / A1 / A1 mini in OctoPrint's
  Control tab
- 🎞️ **SD-card Timelapse Manager** — List, copy, move and delete the printer's
  own timelapse videos over FTPS from a dedicated tab, with thumbnails,
  multi-select, progress/ETA, and automatic `.avi` → `.mp4` conversion (see
  [Manage SD-card timelapses](#manage-sd-card-timelapses))
- 🤖 **Auto-sync after a print** — Optionally pull the new timelapse
  automatically once the print finishes and the system is idle (printer done,
  OctoPrint's own render finished) — copy or move, your choice
- 📸 **Snapshot & Timelapse** — Full `WebcamProviderPlugin` integration for
  OctoPrint's built-in timelapse engine
- 💡 **Light toggle** — Switch the printer's light on/off from a button over the
  live stream (requires Bambu Connector — see [Compatibility](#compatibility))
- 🔌 **Offline-resilient** — When the printer is powered off the stream shows a
  "Printer Offline" frame and reconnects automatically once it is back, without
  tripping the crash limit
- 🔗 **Auto-config from Bambu Connector** — If
  [OctoPrint-BambuConnector](https://github.com/OctoPrint/OctoPrint-BambuConnector)
  is set up, reuse its printer IP and access code instead of entering them twice
- 🔄 **Auto-managed Daemon** — Starts, restarts on crash, and reconfigures
  without any manual setup
- 🔃 **Rotation & Overlays** — Image rotation, FPS watermark, and activity dot
- 🧪 **Connection Test** — Verify IP and access code before saving settings
- 🔒 **Security Hardened** — Access code never exposed via daemon HTTP;
  unauthenticated shutdown endpoint disabled
- 🌐 **Reverse-Proxy Ready** — Stream URL override for setups behind nginx / Caddy
- 🖥️ **Bind Address Control** — `127.0.0.1` (safe default) or `0.0.0.0` for
  browser live view

## Supported printers

<!-- markdownlint-disable MD013 -->

| Series    | Camera stream | Timelapse manager (FTPS) | Light toggle (MQTT) | Tested |
| --------- | :-----------: | :----------------------: | :-----------------: | :----: |
| A1 mini   |      ✅       |            ✅            |         ✅          |   ✅   |
| A1        |      ✅       |            ✅            |         ⚠️          |   —    |
| P1P / P1S |      ✅       |            ✅            |         ⚠️          |   —    |
| X1 / X1C  |      ❌       |            ⚠️            |         ⚠️          |   —    |

<!-- markdownlint-enable MD013 -->

Legend: ✅ works · ⚠️ should work but **untested** on that model · ❌ not
supported. All printers need **LAN mode** with
the access code from the printer's _Network_ settings.

> **Note:** The printer camera delivers roughly **0.5–2 FPS** by design — that
> is a limitation of the printer firmware, not the plugin. If Bambu Studio or
> the Bambu Handy app is watching the camera simultaneously, the connection may
> fail.

## Compatibility

BambuCam requires **OctoPrint ≥ 1.10.0**.

<!-- markdownlint-disable MD033 -->
<details>
<summary>OctoPrint 1.x vs. 2.0 — feature breakdown</summary>
</br>

Almost everything works on the
OctoPrint 1.x series in **manual mode** (you type the printer IP and access
code yourself). Two convenience features build on
[OctoPrint-BambuConnector](https://github.com/OctoPrint/OctoPrint-BambuConnector),
which is part of OctoPrint's 2.0 connector architecture, so they are only
available on **OctoPrint 2.0+ with Bambu Connector set up**.

<!-- markdownlint-disable MD013 -->

| Feature                                              | OctoPrint 1.x | OctoPrint 2.0 + Bambu Connector |
| ---------------------------------------------------- | :-----------: | :-----------------------------: |
| Live MJPEG stream & snapshot                         |      ✅       |               ✅                |
| Auto-managed daemon (restart / offline-resilient)    |      ✅       |               ✅                |
| WebcamProvider (multicam) integration                |      ✅       |               ✅                |
| Manual configuration (IP + access code)              |      ✅       |               ✅                |
| Connection test                                      |      ✅       |               ✅                |
| SD-card timelapse list / thumbnails                  |      ✅       |               ✅                |
| Copy / move / delete timelapses (FTPS)               |      ✅       |               ✅                |
| `.avi` → `.mp4` transcode + ffmpeg indicator         |      ✅       |               ✅                |
| Convert local `.avi` files                           |      ✅       |               ✅                |
| Auto-sync after a print (`MovieDone`)                |      ✅       |               ✅                |
| Rotation, FPS watermark, activity dot                |      ✅       |               ✅                |
| **Auto-config** (reuse Connector IP / access code)   |      ❌       |               ✅                |
| **Light toggle** (needs the printer serial via MQTT) |      ❌       |               ✅                |

<!-- markdownlint-enable MD013 -->

On OctoPrint 1.x the two unavailable features degrade gracefully: the
**Configuration** dropdown stays on `Manual` (the `Auto` option is disabled),
and the light button is hidden. Nothing errors — you just configure the printer
manually and lose the LED control.

</details>
<!-- markdownlint-enable MD033 -->

## Installation

### Via Plugin Manager (Recommended)

1. Open the OctoPrint web interface
2. Navigate to **Settings** → **Plugin Manager**
3. Click **Get More...**
4. Click **Install from URL** and enter:

   ```text
   https://github.com/Ajimaru/OctoPrint-BambuCam/releases/latest/download/OctoPrint-BambuCam-latest.zip
   ```

5. Click **Install**
6. Restart OctoPrint

### Manual Installation

<!-- markdownlint-disable MD033 -->
<details>
<summary>Manual pip install</summary>

```bash
pip install https://github.com/Ajimaru/OctoPrint-BambuCam/releases/latest/download/OctoPrint-BambuCam-latest.zip
```

The `releases/latest` URL always points to the newest stable release.

</details>
<!-- markdownlint-enable MD033 -->

## Configuration

Open **Settings → Plugins → BambuCam**, enter the printer IP and access code
(or pick **Auto** when Bambu Connector is set up), and hit **Test connection**.
Everything else has sensible defaults and is optional.

<!-- markdownlint-disable MD033 -->
<details>
<summary>Required &amp; optional settings</summary>
</br>

| Setting                   | Description                                      |
| ------------------------- | ------------------------------------------------ |
| **Configuration**         | `Manual` (type the values) or `Auto`.            |
| **Printer IP / hostname** | LAN address, e.g. `192.168.1.100`.               |
| **Access code**           | Shown on printer display under _Network_ config. |

Use **Test connection** to verify both values before saving.

> 💡 If [OctoPrint-BambuConnector](https://github.com/OctoPrint/OctoPrint-BambuConnector)
> is installed and configured, choose **Configuration → Auto** to reuse its
> printer IP and access code instead of entering them twice. The fields then
> show those values read-only. If it is not available, BambuCam stays on manual
> entry.

<!-- separate blockquotes -->

> ⚠️ The bundled MJPEG server has **no authentication**. With
> `bind_address = 0.0.0.0`, anyone on your network can watch the camera stream.
> Use `127.0.0.1` (default) unless you need browser live view and have a reverse
> proxy in place.

<h4>Optional settings</h4>

<!-- markdownlint-disable MD013 -->

| Setting             | Default     | Description                                        |
| ------------------- | ----------- | -------------------------------------------------- |
| HTTP port           | `8181`      | Local port of the MJPEG server.                    |
| Bind address        | `127.0.0.1` | `127.0.0.1` = safe. `0.0.0.0` = browser live view. |
| Stream URL override | _(empty)_   | Use with a reverse proxy.                          |
| Override resolution | off         | Off = printer sets the frame size (recommended).   |
| Width / Height      | `1920x1080` | Only applied when Override resolution is enabled.  |
| Rotation            | `-1` (none) | Rotate 90 / 180 / 270 degrees.                     |
| Activity dot        | off         | Overlay a pulsing dot when the stream is active.   |
| FPS watermark       | off         | Overlay the measured FPS on the image.             |
| Auto-restart        | on          | Automatically restart the daemon on crash.         |
| Max restarts        | `5`         | Max restarts within the restart window.            |
| Restart window      | `300 s`     | Time window for counting restarts.                 |
| Filename suffix     | _(empty)_   | Appended to downloaded timelapse names.            |
| Convert to `.mp4`   | on          | Re-encode copied `.avi` to playable `.mp4`.        |
| Auto-sync           | off         | Pull new timelapses automatically after a print.   |
| Auto-pull action    | `copy`      | `copy` (keep on SD) or `move` (delete from SD).    |
| Delay after print   | `60 s`      | Wait before checking the SD card for the new file. |

<!-- markdownlint-enable MD013 -->

</details>
<!-- markdownlint-enable MD033 -->

## Manage SD-card timelapses

P1/A1 printers record timelapses onto their (micro-)SD card. The **BambuCam
Timelapse** tab browses and manages them over FTPS — no need to pull the card
or open Bambu Studio. Each row shows size, date, a clickable preview thumbnail
and a **✓ Copied** badge; checkboxes allow multi-select with per-file progress.

- **Copy** downloads into OctoPrint's `timelapse` folder (SD original kept).
- **Move** copies, then deletes the original **only after** a byte-for-byte
  verify. **Delete** removes from the card after a confirmation.
- Copied/converted videos appear in OctoPrint's native **Timelapse** tab.

Notes and safety:

- **Automatic `.avi` → `.mp4` conversion (on by default).** Bambu records
  Motion-JPEG `.avi`, which many players can't open, so copies are re-encoded to
  H.264 `.mp4` via **OctoPrint's own ffmpeg**. If ffmpeg isn't configured, the
  `.avi` is kept. Leftover `.avi` (off/skipped/failed) can be converted later
  from the **"Local .avi files"** section at the bottom of the tab.
- **Auto-sync (opt-in).** Pull a print's new timelapse automatically once the
  system is idle — **printer stopped AND OctoPrint done rendering** — so two
  ffmpeg encodes never overlap. A tunable delay covers the SD-card write.
- **Move, delete and conversion are blocked while a print is running**; copy is
  always allowed (read-only). All SD writes are **admin-only**, the access code
  never appears in any log, and re-copying asks first then saves a numbered copy
  (`…-1.mp4`).

## Security notes

The access code is never logged or exposed to the browser, printer connections
run over TLS, and privileged SD-card/light actions are permission-gated. See the
**[Security model](SECURITY.md#security-model)** in [SECURITY.md](SECURITY.md)
for the full posture and how to report a vulnerability.

## How it works

For the **live stream**, the plugin bundles and supervises
[webcamd-bambu](https://github.com/disconn3ct/webcamd-bambu) (`bambu` branch),
a small MJPEG HTTP server that connects to the printer's camera port (TCP 6000,
TLS) using the LAN access code. BambuCam starts the daemon automatically after
OctoPrint boots, monitors it, restarts it after crashes, and reconfigures it
when you change settings.

The other features talk to the printer directly from the plugin, no daemon
involved: **timelapse management** over FTPS (TCP 990) and the **light toggle**
over the printer's local MQTT broker (TCP 8883) — both using the same LAN access
code.

## Contributing

Contributions are welcome! Please see [CONTRIBUTING.md](CONTRIBUTING.md) for
detailed guidelines and instructions.

Please also follow our [Code of Conduct](CODE_OF_CONDUCT.md).

## License

AGPL-3.0-or-later — see [LICENSE](LICENSE) for details.

This plugin bundles
[`webcam.py` from webcamd-bambu](https://github.com/disconn3ct/webcamd-bambu)
(`bambu` branch), licensed under **GPL-3.0**. See [AUTHORS.md](AUTHORS.md) for
the full upstream author list and [CHANGELOG.md](CHANGELOG.md) for release
history.

## Support

- 🐛 **Bug Reports**: [GitHub Issues][issues]
- 💬 **Discussion**: [GitHub Discussions][discussions]

[issues]: https://github.com/Ajimaru/OctoPrint-BambuCam/issues
[discussions]: https://github.com/Ajimaru/OctoPrint-BambuCam/discussions

For troubleshooting, check the BambuCam log at
**Settings → Logging → octoprint.plugins.bambucam** and attach the OctoPrint
systeminfo bundle when opening a bug report.

## Credits

- **Development**: Built following
  [OctoPrint Plugin Guidelines](https://docs.octoprint.org/en/main/plugins/index.html)
- **Upstream daemon**: [webcamd-bambu](https://github.com/disconn3ct/webcamd-bambu)
  by Igor Maculan, Christopher RYU, Shell Shrader, disconn3ct (SMS)
- **Contributors**: See [AUTHORS.md](AUTHORS.md)

## 100% Badge Coverage

<!-- markdownlint-disable MD033 -->
<details>
<summary>Show all badges</summary>

### 🏗️ 1. Build & Test Status

[![CI][b-ci]](https://github.com/Ajimaru/OctoPrint-BambuCam/actions/workflows/ci.yml?query=branch%3Amain)
[![Docs workflow][b-docs]](https://github.com/Ajimaru/OctoPrint-BambuCam/actions/workflows/docs.yml?query=branch%3Amain)
[![i18n][b-i18n]](https://github.com/Ajimaru/OctoPrint-BambuCam/actions/workflows/i18n.yml?query=branch%3Amain)
[![Lint][b-lint]](https://github.com/Ajimaru/OctoPrint-BambuCam/actions/workflows/lint.yml?query=branch%3Amain)
[![Bandit SARIF][b-bandit]](https://github.com/Ajimaru/OctoPrint-BambuCam/actions/workflows/bandit-sarif.yml?query=branch%3Amain)

[b-ci]: https://img.shields.io/github/actions/workflow/status/Ajimaru/OctoPrint-BambuCam/ci.yml?branch=main&style=flat-square&label=CI
[b-docs]: https://img.shields.io/github/actions/workflow/status/Ajimaru/OctoPrint-BambuCam/docs.yml?branch=main&style=flat-square&label=docs
[b-i18n]: https://img.shields.io/github/actions/workflow/status/Ajimaru/OctoPrint-BambuCam/i18n.yml?branch=main&style=flat-square&label=i18n
[b-lint]: https://img.shields.io/github/actions/workflow/status/Ajimaru/OctoPrint-BambuCam/lint.yml?branch=main&style=flat-square&label=Lint
[b-bandit]: https://img.shields.io/github/actions/workflow/status/Ajimaru/OctoPrint-BambuCam/bandit-sarif.yml?branch=main&style=flat-square&label=Bandit%20SARIF

### 🧪 2. Code Quality & Formatting

[![Code style: black][b-black]](https://github.com/psf/black)
[![Imports: isort][b-isort]](https://pycqa.github.io/isort/)
[![Prettier][b-prettier]](https://github.com/prettier/prettier)
[![pre-commit][b-precommit]](https://pre-commit.com/)
[![Codacy][b-codacy]](https://app.codacy.com/gh/Ajimaru/OctoPrint-BambuCam/dashboard)
[![Coverage][b-coverage]](https://codecov.io/gh/Ajimaru/OctoPrint-BambuCam)
[![Pylint Score][b-pylint]](https://www.pylint.org/)
[![Bandit Security][b-sec]](https://bandit.readthedocs.io/en/latest/)
[![Depfu][b-depfu]](https://depfu.com/)
[![Known Vulnerabilities][b-snyk]](https://snyk.io/test/github/Ajimaru/OctoPrint-BambuCam)

[b-black]: https://img.shields.io/badge/code%20style-black-000000.svg?style=flat-square
[b-isort]: https://img.shields.io/badge/%20imports-isort-%231674b1?style=flat-square&labelColor=ef8336
[b-prettier]: https://img.shields.io/badge/code_style-prettier-ff69b4.svg?style=flat-square
[b-precommit]: https://img.shields.io/badge/pre--commit-enabled-brightgreen?style=flat-square&logo=pre-commit&logoColor=white
[b-codacy]: https://img.shields.io/codacy/grade/75d9ec1b49a64a3aae3615e21e6ff2ce?style=flat-square
[b-coverage]: https://img.shields.io/codecov/c/github/Ajimaru/OctoPrint-BambuCam?style=flat-square
[b-pylint]: https://img.shields.io/badge/pylint-10.0-green.svg?style=flat-square
[b-sec]: https://img.shields.io/badge/bandit-security-green.svg?style=flat-square
[b-depfu]: https://badges.depfu.com/badges/7d0d03953a51f03e18a2eae2453d64f5/status.svg
[b-snyk]: https://snyk.io/test/github/Ajimaru/OctoPrint-BambuCam/badge.svg

### 🔄 3. CI/CD & Release

[![SemVer][b-semver]](https://semver.org/)
[![Release Date][b-reldate]](https://github.com/Ajimaru/OctoPrint-BambuCam/releases)
[![Latest Release][b-latest]](https://github.com/Ajimaru/OctoPrint-BambuCam/releases/latest)
[![Downloads][b-dl]](https://github.com/Ajimaru/OctoPrint-BambuCam/releases)
[![Pre‑Release][b-pre]](https://github.com/Ajimaru/OctoPrint-BambuCam/releases)
[![Python][b-py]](https://python.org)
[![OctoPrint][b-op]](https://octoprint.org)
[![Maintenance][b-maint]](https://github.com/Ajimaru/OctoPrint-BambuCam/graphs/commit-activity)

[b-semver]: https://img.shields.io/badge/semver-2.0.0-blue?style=flat-square
[b-reldate]: https://img.shields.io/github/release-date/Ajimaru/OctoPrint-BambuCam?style=flat-square
[b-latest]: https://img.shields.io/github/v/release/Ajimaru/OctoPrint-BambuCam?style=flat-square&sort=semver
[b-dl]: https://img.shields.io/github/downloads/Ajimaru/OctoPrint-BambuCam/total.svg?style=flat-square
[b-pre]: https://img.shields.io/github/v/release/Ajimaru/OctoPrint-BambuCam?style=flat-square&include_prereleases&label=pre-release
[b-py]: https://img.shields.io/badge/python-3.9%2B-blue.svg?style=flat-square
[b-op]: https://img.shields.io/badge/OctoPrint-1.10.0%2B-blue.svg?style=flat-square
[b-maint]: https://img.shields.io/maintenance/yes/2026?style=flat-square

### 📊 4. Repository Activity

[![Open Issues][b-oi]](https://github.com/Ajimaru/OctoPrint-BambuCam/issues?q=is%3Aissue%20state%3Aopen)
[![Closed Issues][b-ci2]](https://github.com/Ajimaru/OctoPrint-BambuCam/issues?q=is%3Aissue%20state%3Aclosed)
[![Open PRs][b-opr]](https://github.com/Ajimaru/OctoPrint-BambuCam/pulls?q=is%3Apr+is%3Aopen)
[![Closed PRs][b-cpr]](https://github.com/Ajimaru/OctoPrint-BambuCam/pulls?q=is%3Apr+is%3Aclosed)
[![Last Commit][b-lc]](https://github.com/Ajimaru/OctoPrint-BambuCam/commits/main)
[![Commit Activity][b-ca]](https://github.com/Ajimaru/OctoPrint-BambuCam/graphs/commit-activity)
[![Contributors][b-con]](https://github.com/Ajimaru/OctoPrint-BambuCam/graphs/contributors)

[b-oi]: https://img.shields.io/github/issues/Ajimaru/OctoPrint-BambuCam?style=flat-square
[b-ci2]: https://img.shields.io/github/issues-closed-raw/Ajimaru/OctoPrint-BambuCam?style=flat-square
[b-opr]: https://img.shields.io/github/issues-pr/Ajimaru/OctoPrint-BambuCam?style=flat-square
[b-cpr]: https://img.shields.io/github/issues-pr-closed/Ajimaru/OctoPrint-BambuCam?style=flat-square
[b-lc]: https://img.shields.io/github/last-commit/Ajimaru/OctoPrint-BambuCam?style=flat-square
[b-ca]: https://img.shields.io/github/commit-activity/y/Ajimaru/OctoPrint-BambuCam?style=flat-square
[b-con]: https://img.shields.io/github/contributors/Ajimaru/OctoPrint-BambuCam?style=flat-square

### 🧾 5. Metadata

![Code Size][b-size]
[![Security][b-secp]](https://github.com/Ajimaru/OctoPrint-BambuCam/blob/main/SECURITY.md)
[![Snyk][b-snyks]](https://app.snyk.io)
![Languages Count][b-langc]
![Top Language][b-top]
[![License][b-lic]](https://github.com/Ajimaru/OctoPrint-BambuCam/blob/main/LICENSE)
[![PRs Welcome][b-prs]](https://github.com/Ajimaru/OctoPrint-BambuCam/pulls)

[b-size]: https://img.shields.io/github/languages/code-size/Ajimaru/OctoPrint-BambuCam?style=flat-square
[b-secp]: https://img.shields.io/badge/security-policy-blue?style=flat-square
[b-snyks]: https://img.shields.io/badge/security-snyk-blueviolet?style=flat-square
[b-langc]: https://img.shields.io/github/languages/count/Ajimaru/OctoPrint-BambuCam?style=flat-square
[b-top]: https://img.shields.io/github/languages/top/Ajimaru/OctoPrint-BambuCam?style=flat-square
[b-lic]: https://img.shields.io/github/license/Ajimaru/OctoPrint-BambuCam?style=flat-square
[b-prs]: https://img.shields.io/badge/PRs-welcome-brightgreen.svg?style=flat-square

</details>
<!-- markdownlint-enable MD033 -->

---

![Stars][b-stars] ![Forks][b-forks] ![Watchers][b-watch]

[b-stars]: https://img.shields.io/github/stars/Ajimaru/OctoPrint-BambuCam?style=social
[b-forks]: https://img.shields.io/github/forks/Ajimaru/OctoPrint-BambuCam?style=social
[b-watch]: https://img.shields.io/github/watchers/Ajimaru/OctoPrint-BambuCam?style=social

**Like this plugin?** ⭐ Star the repo and share it with the OctoPrint community!
