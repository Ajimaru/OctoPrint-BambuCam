# Getting started

This page covers a local **development** setup. End users should install the
plugin through OctoPrint's Plugin Manager — see the
[README](https://github.com/Ajimaru/OctoPrint-BambuCam#installation).

## Prerequisites

- Python 3.9+ (tested on 3.9–3.13 in CI)
- An OctoPrint instance (a dedicated venv is recommended)
- Node.js 20+ (only for the JS lint/format hooks and JSDoc generation)
- A Bambu Lab P1P / P1S / A1 / A1 mini in **LAN mode** with its access code

## Clone & install

```bash
git clone https://github.com/Ajimaru/OctoPrint-BambuCam.git
cd OctoPrint-BambuCam

python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

# editable install with dev + test extras
pip install -e ".[develop]"
pip install pytest pytest-cov pre-commit
npm install --no-save              # ESLint / Prettier / jsdoc2md

pre-commit install
```

## Run the tests

```bash
pytest tests/ -v
```

The suite enforces **90 % coverage** (`--cov-fail-under=90`, see
`pyproject.toml`).

## Run inside OctoPrint

Install the plugin into the same environment as an OctoPrint instance and start
OctoPrint. Configure the printer IP and access code under
**Settings → Plugins → BambuCam**, then use **Test connection** before saving.

```bash
octoprint serve
```

The plugin starts the bundled daemon automatically after OctoPrint finishes
booting (`on_after_startup`), provided `enabled` is set and both `hostname` and
`access_code` are configured.

## Next steps

- [Architecture overview](architecture/overview.md)
- [Configuration reference](reference/configuration.md)
- [Contributing](development/contributing.md)
