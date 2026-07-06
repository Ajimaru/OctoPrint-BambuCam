# Configuration reference

All settings keys, their defaults (`get_settings_defaults`) and effect. Keys
marked **↻** trigger a daemon restart when changed (they are members of
`DAEMON_SETTINGS`).

| Key                   | Default     |  ↻  | Description                                                   |
| --------------------- | ----------- | :-: | ------------------------------------------------------------- |
| `enabled`             | `True`      | ✅  | Master switch; when off the daemon is stopped.                |
| `config_source`       | `"manual"`  |  —  | `"manual"` or `"auto"` (reuse Bambu Connector's IP/code).     |
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
| `download_suffix`     | `""`        |  —  | Suffix added to downloaded timelapse names (e.g. `_bambu`).   |
| `transcode_to_mp4`    | `True`      |  —  | Re-encode copied `.avi` to playable `.mp4` via ffmpeg.        |
| `auto_sync`           | `False`     |  —  | Pull a new timelapse automatically after a print.             |
| `auto_sync_delay`     | `420`       |  —  | Seconds to wait after a print before checking the SD card.    |
| `auto_sync_action`    | `"copy"`    |  —  | `"copy"` (keep on SD) or `"move"` (delete from SD).           |
| `auto_sync_measure`   | `False`     |  —  | Debug: log render-delay after `PrintDone` (see note below).   |
| `print_dates`         | `{}`        |  —  | Internal: SD video name → real print-end time (note below).   |

!!! note "Printer offline is not a crash"

    When the printer is powered off or unreachable, the daemon exits with a
    distinct code and the supervisor reconnects on a calm fixed interval (30 s)
    **without** counting it against `max_restarts`/`restart_window` and without
    ever giving up — the stream shows a "Printer Offline" frame and recovers
    automatically once the printer returns. `max_restarts` and `restart_window`
    therefore only ever bound _real_ crashes. Disabling `autorestart` also
    disables this reconnect. See
    [Printer-offline handling](../architecture/daemon.md#printer-offline-handling).

!!! note "Why `auto_sync_delay` defaults to 420 s"

    The printer renders its timelapse video on the SD card with a delay. A
    measurement on an A1 mini saw the `.avi` first appear **+352 s** after
    `PrintDone` and keep growing until **+370 s**; the printer often finishes
    rendering *during* the print, but not always. The default of `420` s
    (7 min) covers that worst case with margin. Syncing too early would copy a
    half-written file. Enable `auto_sync_measure` to re-measure on your own
    hardware — it logs `render-delay measure: … Suggested auto_sync_delay >= N s`
    lines you can use to tune the value.

!!! warning "Timelapse dates on the SD card can be wrong"

    Some Bambu firmware in LAN-only mode stamps **everything** on the SD card —
    the video filename, the FTP `MDTM` timestamp, the thumbnail, even the logs —
    with a frozen, incorrect date from the camera subsystem's clock. Nothing on
    the card links a video to its real time, and guessing a correction produced
    plausible-yet-wrong dates, so BambuCam never fabricates a date. Instead it
    shows a real date only when it actually has one, from two trustworthy
    sources, best first:

    1. **A copied file** → its real local copy time (the copy's mtime is stamped
       at copy time).
    2. **An uncopied file we recorded** → the real print-end time. On
       `PrintStarted` the plugin snapshots the SD card; on `PrintDone` it waits
       for the printer to finish rendering (the A1 mini often renders *during*
       the print, stalling at ~98 %), finds the new/grown video, and stores
       `name → time` in `print_dates`. This works even with auto-sync off and
       survives restarts.

    When neither source has a date (e.g. an old video that predates the plugin),
    the tab shows **"Date unknown"** rather than the bogus camera-clock date.

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
