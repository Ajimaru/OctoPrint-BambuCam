# Configuration reference

All settings keys, their defaults (`get_settings_defaults`) and effect. Keys
marked **↻** trigger a daemon restart when changed (they are members of
`DAEMON_SETTINGS`).

| Key                   | Default     |  ↻  | Description                                                   |
| --------------------- | ----------- | :-: | ------------------------------------------------------------- |
| `enabled`             | `True`      | ✅  | Master switch; when off the daemon is stopped.                |
| `hostname`            | `""`        | ✅  | Printer LAN IP / hostname.                                    |
| `access_code`         | `""`        | ✅  | LAN access code (admin-restricted, never sent to non-admins). |
| `port`                | `8181`      | ✅  | Local TCP port of the MJPEG server.                           |
| `bind_address`        | `127.0.0.1` | ✅  | `127.0.0.1` (safe) or `0.0.0.0` (browser live view).          |
| `stream_url_override` | `""`        |  —  | Explicit stream URL for reverse-proxy setups (no restart).    |
| `width`               | `1920`      | ✅  | Capture width passed to `--width`.                            |
| `height`              | `1080`      | ✅  | Capture height passed to `--height`.                          |
| `rotate`              | `-1`        | ✅  | `-1` = none; `0/90/180/270` rotate the image.                 |
| `flashred`            | `False`     | ✅  | Overlay a pulsing activity dot (`--flashred`).                |
| `showfps`             | `False`     | ✅  | Overlay measured FPS watermark (`--showfps`).                 |
| `loghttp`             | `False`     | ✅  | Log HTTP requests to a dedicated rotating file (`--loghttp`). |
| `encodewait`          | `0.5`       | ✅  | Encode loop wait passed to `--encodewait`.                    |
| `autorestart`         | `True`      | ✅  | Restart the daemon after unexpected exits.                    |
| `max_restarts`        | `5`         | ✅  | Max restarts allowed within `restart_window`.                 |
| `restart_window`      | `300`       | ✅  | Sliding window (seconds) for counting restarts.               |

## Restricted paths

`get_settings_restricted_paths` restricts `access_code` to the **admin** scope.

## CLI mapping

`_build_argv()` maps the config dict to `webcam.py` flags. Notably:

- `rotate == -1` omits `--rotate` entirely.
- `width` / `height` / `encodewait` are only passed when set.
- `flashred` / `showfps` / `loghttp` are boolean flags (present when true).
- `bind_address` maps to `--v4bindaddress`, `access_code` to `--password`.
