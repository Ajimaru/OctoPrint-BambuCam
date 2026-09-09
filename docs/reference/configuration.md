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
| `rotate`              | `-1`        | ✅  | `-1` = none; rotate by 1–359 degrees (PIL).                   |
| `flashred`            | `False`     | ✅  | Overlay a pulsing activity dot (`--flashred`).                |
| `showfps`             | `False`     | ✅  | Overlay measured FPS watermark (`--showfps`).                 |
| `loghttp`             | `False`     | ✅  | Log HTTP requests to a dedicated rotating file (`--loghttp`). |
| `encodewait`          | `0.5`       | ✅  | Pause between live-stream frames (FPS cap).                   |
| `autorestart`         | `True`      | ✅  | Restart after crashes; also enables offline reconnect.        |
| `max_restarts`        | `5`         | ✅  | Max crashes allowed within `restart_window`.                  |
| `restart_window`      | `300`       | ✅  | Sliding window (seconds) for counting crashes.                |
| `download_suffix`     | `""`        |  —  | Suffix added to downloaded timelapse names (e.g. `_bambu`).   |
| `prefix_job_name`     | `True`      |  —  | Prefix downloads with the print job's file name (note below). |
| `transcode_to_mp4`    | `True`      |  —  | Re-encode copied `.avi` to playable `.mp4` via ffmpeg.        |
| `sd_thumb_from_gcode` | `False`     |  —  | Use the job's gcode preview as the SD-timelapse thumbnail.    |
| `auto_sync`           | `False`     |  —  | Pull a new timelapse automatically after a print.             |
| `auto_sync_delay`     | `420`       |  —  | Seconds to wait after a print before checking the SD card.    |
| `auto_sync_action`    | `"copy"`    |  —  | `"copy"` (keep on SD) or `"move"` (delete from SD).           |
| `print_dates`         | `{}`        |  —  | Internal: SD video name → real print-end time (note below).   |
| `print_jobs`          | `{}`        |  —  | Internal: SD video name → gcode job name (labels harvests).   |

!!! note "Printer offline is not a crash"

    When the printer is powered off or unreachable, the daemon exits with a
    distinct code and the supervisor reconnects on a calm fixed interval (30 s)
    **without** counting it against `max_restarts`/`restart_window` and without
    ever giving up — the stream shows a "Printer Offline" frame and recovers
    automatically once the printer returns. `max_restarts` and `restart_window`
    therefore only ever bound _real_ crashes. Disabling `autorestart` also
    disables this reconnect. See
    [Printer-offline handling](../architecture/daemon.md#printer-offline-handling).

!!! note "Job-name prefix for downloaded videos"

    With `prefix_job_name` on, a downloaded video is named
    `<job>_<sd-name><suffix>.<ext>`, e.g.
    `benchy.gcode.3mf_video_2026-05-18_08-07-44_A1mini.mp4`. The job name comes
    from the `print_jobs` map captured at `PrintDone` (the same capture as
    `print_dates`), so only videos whose print this plugin observed get a
    prefix; files copied before the option existed remain recognized under
    their un-prefixed name.

!!! note "Why `auto_sync_delay` defaults to 420 s"

    The printer renders its timelapse video on the SD card with a delay. A
    measurement on an A1 mini saw the `.avi` first appear **+352 s** after
    `PrintDone` and keep growing until **+370 s**; the printer often finishes
    rendering *during* the print, but not always. The default of `420` s
    (7 min) covers that worst case with margin. Syncing too early would copy a
    half-written file.

    Note that the delay covers the *printer* finishing its write; waiting for
    OctoPrint's own timelapse render is a separate concern, handled by the idle
    gate in `autosync.py` (printer not printing, OctoPrint not rendering, no
    manual FTP batch in flight). On a printer that never renders a timelapse
    to its SD card the delay still matters — it bounds how long the `/ipcam`
    ring buffer has to settle before a harvest reads it.

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

## Raw Files render pipeline

Settings backing the [Raw Files render pipeline](../architecture/render-pipeline.md)
(the **Raw Files Render** section of the settings dialog). None of these
restart the daemon.

| Key                      | Default       | Description                                                          |
| ------------------------ | ------------- | -------------------------------------------------------------------- |
| `render_enabled`         | `True`        | Master switch for the render pipeline (queue, recovery, retention).  |
| `render_tab_visible`     | `True`        | Show the **Raw Files** subtab in the UI (cosmetic only).             |
| `auto_download_ipcam`    | `False`       | Harvest this print's `/ipcam` chunks automatically after a print.    |
| `auto_render_new_groups` | `False`       | Queue a render (default preset) once a group downloads completely.   |
| `render_only_when_idle`  | `True`        | Hold queued render jobs while the printer is printing/paused.        |
| `default_preset`         | `"fast_720p"` | Preset preselected in the UI and used by auto-render.                |
| `ffmpeg_path`            | `""`          | Override for the render ffmpeg; empty = OctoPrint's `webcam.ffmpeg`. |
| `ffprobe_path`           | `""`          | Override for ffprobe; empty = derived from the ffmpeg location.      |
| `ffmpeg_threads`         | `1`           | `-threads` for encodes; `1` keeps a Pi responsive, `0` = all cores.  |
| `render_timeout`         | `0`           | Seconds before a render job is killed; `0` = built-in default.       |
| `max_queue_size`         | `10`          | Maximum number of queued render jobs.                                |
| `stale_lock_timeout`     | `86400`       | Seconds after which a crashed job's lockfile is reclaimed.           |
| `chunks_retention_days`  | `0`           | Age out **rendered** groups' chunks after N days; `0` = keep.        |
| `move_to_trash`          | `True`        | Retention soft-deletes to `trash/` first instead of removing.        |
| `raw_thumb_from_gcode`   | `False`       | Use the slicer plate preview as the rendered clip's thumbnail.       |

!!! note "gcode-preview thumbnails"

    With `raw_thumb_from_gcode` on, a rendered clip's Timelapse-tab thumbnail
    is the print job's slicer plate preview (Bambu Connector's `plate_1.png`),
    matched by the print-id's gcode stem. When no preview is found the render
    falls back to the clip's last video frame.

!!! note "OctoPrint backups"

    The render pipeline's bulky working directories (`render/raw/chunks/`,
    `render/work/`, `render/trash/` under the plugin data folder) are
    **excluded from OctoPrint backups** — raw chunks can grow to several GB
    and are re-harvestable from the printer's `/ipcam` folder. The small
    `thumbs/` and `metadata/` directories stay in the backup; excluding
    "timelapse" in the backup dialog drops the whole `render/` tree.

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
