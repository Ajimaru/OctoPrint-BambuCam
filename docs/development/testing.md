# Testing

Tests live under `tests/` and run with `pytest`.

```bash
pytest tests/ -v
```

## Coverage gate

`pyproject.toml` configures coverage and a hard minimum:

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "--cov=octoprint_bambucam --cov-report=term-missing --cov-fail-under=90"
```

A run below **90 %** coverage fails.

## Layout

| File                            | Covers                                          |
| ------------------------------- | ----------------------------------------------- |
| `tests/conftest.py`             | Shared fixtures / mocks.                        |
| `tests/test_plugin.py`          | `BambucamPlugin` mixin behaviour and the API.   |
| `tests/test_daemon.py`          | `WebcamdManager` lifecycle, watchdog, probe.    |
| `tests/test_timelapse_api.py`   | Timelapse/LED API commands and batch transfers. |
| `tests/test_ftp.py`             | FTPS client (implicit TLS, list/download).      |
| `tests/test_mqtt.py`            | MQTT light client and error classification.     |
| `tests/test_transcode.py`       | ffmpeg `.avi` → `.mp4` transcoder.              |
| `tests/test_autosync.py`        | After-print auto-sync gating.                   |
| `tests/test_bambu_connector.py` | Connector auto-config discovery.                |

## Mocking strategy

The daemon supervisor spawns a real subprocess in production, so tests use
[`pytest-mock`](https://pypi.org/project/pytest-mock/) to patch
`subprocess.Popen`, sockets, and `urllib.request` — exercising the
start/stop/restart and generation/backoff logic without launching `webcam.py`
or touching a printer.

## CI

The `ci` workflow (`.github/workflows/ci.yml`) runs the suite; `lint`,
`bandit-sarif`, `codeql` and `i18n` workflows cover style, security and
catalogs.
