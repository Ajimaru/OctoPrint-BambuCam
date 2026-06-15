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
- 🔄 **Auto-managed Daemon** — Starts, restarts on crash, and reconfigures
  without any manual setup
- 📸 **Snapshot & Timelapse** — Full `WebcamProviderPlugin` integration for
  OctoPrint's built-in timelapse engine
- 🧪 **Connection Test** — Verify IP and access code before saving settings
- 🔃 **Rotation & Overlays** — Image rotation, FPS watermark, and activity dot
- 🔒 **Security Hardened** — Access code never exposed via daemon HTTP;
  unauthenticated shutdown endpoint disabled
- 🌐 **Reverse-Proxy Ready** — Stream URL override for setups behind nginx / Caddy
- 🖥️ **Bind Address Control** — `127.0.0.1` (safe default) or `0.0.0.0` for
  browser live view

## Supported printers

<!-- markdownlint-disable MD013 -->

| Series       | Supported | Notes                                                    |
| ------------ | :-------: | -------------------------------------------------------- |
| P1P / P1S    |    ✅     | LAN mode, access code required                           |
| A1 / A1 mini |    ✅     | LAN mode, access code required                           |
| X1 / X1C     |    ❌     | Uses RTSPS (port 322), different protocol — out of scope |

<!-- markdownlint-enable MD013 -->

> **Note:** The printer camera delivers roughly **0.5–2 FPS** by design — that
> is a limitation of the printer firmware, not the plugin. If Bambu Studio or
> the Bambu Handy app is watching the camera simultaneously, the connection may
> fail.

## Installation

### Via Plugin Manager (Recommended)

1. Open the OctoPrint web interface
2. Navigate to **Settings** → **Plugin Manager**
3. Click **Get More...**
4. Click **Install from URL** and enter:

   ```text
   https://github.com/Ajimaru/OctoPrint-BambuCam/releases/latest/download/BambuCam-latest.whl
   ```

5. Click **Install**
6. Restart OctoPrint

### Manual Installation

<!-- markdownlint-disable MD033 -->
<details>
<summary>Manual pip install</summary>

```bash
pip install https://github.com/Ajimaru/OctoPrint-BambuCam/releases/latest/download/BambuCam-latest.whl
```

The `releases/latest` URL always points to the newest stable release.

</details>
<!-- markdownlint-enable MD033 -->

## Configuration

Open **Settings → Plugins → BambuCam** and fill in:

| Setting                   | Description                                      |
| ------------------------- | ------------------------------------------------ |
| **Printer IP / hostname** | LAN address, e.g. `192.168.1.100`.               |
| **Access code**           | Shown on printer display under _Network_ config. |

Use **Test connection** to verify both values before saving.

> ⚠️ The bundled MJPEG server has **no authentication**. With
> `bind_address = 0.0.0.0`, anyone on your network can watch the camera stream.
> Use `127.0.0.1` (default) unless you need browser live view and have a reverse
> proxy in place.

### Optional settings

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

<!-- markdownlint-enable MD013 -->

## Security notes

- The access code is stored in OctoPrint's settings (admin-restricted) and
  passed to the daemon on its command line, so it is visible in the local
  process list.
- The vendored daemon is patched so that the access code is **never exposed**
  via its `/?info` endpoint and the unauthenticated `/?shutdown` endpoint is
  **disabled** (see `octoprint_bambucam/vendor/webcamd_bambu/UPSTREAM.md`).
- The snapshot and timelapse paths always connect via loopback (`127.0.0.1`)
  regardless of the bind address setting.

## How it works

The plugin bundles and supervises
[webcamd-bambu](https://github.com/disconn3ct/webcamd-bambu) (`bambu` branch),
a small MJPEG HTTP server that connects to the printer's camera port (TCP 6000,
TLS) using the LAN access code. BambuCam starts the daemon automatically after
OctoPrint boots, monitors it, restarts it after crashes, and reconfigures it
when you change settings.

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
