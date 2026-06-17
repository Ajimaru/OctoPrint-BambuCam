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
| `override_resolution` | `False`     | ✅  | Opt-in to force `--width`/`--height` (off = printer-native).  |
| `width`               | `1920`      | ✅  | `--width`, only sent when `override_resolution` is on.        |
| `height`              | `1080`      | ✅  | `--height`, only sent when `override_resolution` is on.       |
| `rotate`              | `-1`        | ✅  | `-1` = none; `0/90/180/270` rotate the image.                 |
| `flashred`            | `False`     | ✅  | Overlay a pulsing activity dot (`--flashred`).                |
| `showfps`             | `False`     | ✅  | Overlay measured FPS watermark (`--showfps`).                 |
| `loghttp`             | `False`     | ✅  | Log HTTP requests to a dedicated rotating file (`--loghttp`). |
| `encodewait`          | `0.5`       | ✅  | Encode loop wait passed to `--encodewait`.                    |
| `autorestart`         | `True`      | ✅  | Restart after crashes; also enables offline reconnect.        |
| `max_restarts`        | `5`         | ✅  | Max crashes allowed within `restart_window`.                  |
| `restart_window`      | `300`       | ✅  | Sliding window (seconds) for counting crashes.                |

!!! note "Printer offline is not a crash"
When the printer is powered off or unreachable, the daemon exits with a
distinct code and the supervisor reconnects on a calm fixed interval (30 s)
**without** counting it against `max_restarts`/`restart_window` and without
ever giving up — the stream shows a "Printer Offline" frame and recovers
automatically once the printer returns. `max_restarts` and `restart_window`
therefore only ever bound _real_ crashes. Disabling `autorestart` also
disables this reconnect. See
[Printer-offline handling](../architecture/daemon.md#printer-offline-handling).

## Restricted paths

`get_settings_restricted_paths` restricts `access_code` to the **admin** scope.

## CLI mapping

`_build_argv()` maps the config dict to `webcam.py` flags. Notably:

- `rotate == -1` omits `--rotate` entirely.
- `width` / `height` are only passed when `override_resolution` is enabled
  (and the value is non-zero); otherwise the printer's native frame size is used.
- `encodewait` is only passed when set.
- `flashred` / `showfps` / `loghttp` are boolean flags (present when true).
- `bind_address` maps to `--v4bindaddress`, `access_code` to `--password`.
