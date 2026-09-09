# UI placements

The frontend viewmodel (`BambuCam.js`) binds three OctoPrint UI surfaces.

## Settings dialog

Template: `bambucam_settings.jinja2`, bound to `#settings_plugin_bambucam`.
Split into five tabs:

- **Connection** — a Configuration dropdown (`Manual` / `Auto` from Bambu
  Connector; `Auto` is disabled when the connector is unavailable), printer IP /
  hostname and access code (read-only in auto mode), and a **Test connection**
  button (`testConnection`).
- **Webcam Image** — resolution override, rotation, FPS watermark and
  activity-dot overlays for the live webcam stream.
- **Printer Timelapse** — the `.avi` → `.mp4` conversion toggle with an
  **ffmpeg indicator** (`fetchFfmpegStatus`), a **"Use the print job's gcode
  preview as the thumbnail"** toggle (`sd_thumb_from_gcode`), plus the auto-sync
  controls for the timelapse the printer records itself.
- **Raw Files Render** — the render pipeline settings (auto-download,
  auto-render, a **gcode-preview thumbnail** toggle (`raw_thumb_from_gcode`),
  a **Show the "Raw Files" subtab** toggle (`render_tab_visible`, cosmetic
  only), presets, render-queue performance, retention). The queue-scoped performance
  options — **Render only when idle**, **Max queue size**, **Render timeout** —
  apply to the raw-chunk render only. See the
  [configuration reference](../reference/configuration.md#raw-files-render-pipeline).
- **Advanced** — daemon (port / bind address / restart policy), the
  **ffmpeg/ffprobe path overrides** and **ffmpeg threads** (these govern _all_
  the plugin's ffmpeg work — both the raw render and the printer-timelapse
  `.avi`→`.mp4` conversion — so they live here, not in the render tab; empty
  paths fall back to OctoPrint's webcam ffmpeg), and diagnostics: the **Fetch
  info** button (`fetchInfo`) and **Restart**
  (`restartDaemon`), plus copyable snapshot/stream URLs.

Each option's help text lives in a small **"?" tooltip** beside its label
(hover or keyboard-focus to reveal it — the `help()` macro at the top of the
template). The bubble is positioned by `_positionHelpTooltip` in
`BambuCam.js`: centered above the mouse pointer (or the icon, on keyboard
focus), clamped to the viewport so it can't be clipped at the dialog edges,
flipping below the icon when there is no room above.

The two options that copy data off the printer's SD
card — **Automatically pull new timelapses** (Printer Timelapse tab) and
**Auto-download /ipcam chunks** (Raw Files Render tab) — additionally keep a
persistent amber **info box** under the toggle warning that the transfer is slow
and can make the printer's touchscreen sluggish; the rest of their help is in
the tooltip.

The status panel (running state, PID, encode FPS, session count, last error) is
polled every 10 s while the dialog is open (`onSettingsShown` /
`onSettingsHidden`), which also runs `detectConnector` and `fetchFfmpegStatus`.

## Webcam tab

Template: `bambucam_webcam.jinja2`, bound to `#bambucam_webcam_container`.

Renders the MJPEG `<img id="bambucam_stream">` plus a **light toggle** button
overlaid on the stream (`toggleLed`, shown only when `ledAvailable`). The
viewmodel:

- computes the stream `src` (`streamUrl`), appending a cache-buster.
- shows a load/error state (`onStreamLoaded` / `onStreamErrored`).
- **retries** every 10 s on error (the daemon may still be starting).
- reloads the stream 2 s after settings are saved or the daemon reports
  `started`.

## BambuCam tab

Template: `bambucam_tab.jinja2`, bound to `#tab_plugin_bambucam`. Holds two
subtabs, **Timelapse** and **Raw Files**. This is the plugin's only tab —
`bambucam_raw.jinja2` is included by this template rather than registered as a
tab of its own, so it renders inside the bound element.

### Timelapse subtab

The SD-card timelapse manager: a list (preview thumbnail, details, status) with
multi-select and a sort menu, **Copy / Move / Delete** batch actions with live
progress, and a **"Local .avi files"** section for converting leftovers. Lazily
loads the list the first time the tab is shown (`onTabChange`).

While a post-print copy/harvest pipeline is running, a blue **"Copy in
progress"** banner shows and the automatic list refresh is held (so it can't
disturb the printer's single FTPS session); the list refreshes automatically
once the pipeline finishes. A user-initiated **Refresh** still works during the
hold.

### Raw Files subtab

Template: `bambucam_raw.jinja2` (included in `bambucam_tab.jinja2`). The raw
`/ipcam` footage manager — see the
[render pipeline](../architecture/render-pipeline.md).

- Toolbar: **Reload Library** (rescan `raw/chunks/`, local only) and
  **Fetch from printer** (manual `/ipcam` harvest, admin-only). Both are
  disabled while a print runs or the pipeline is busy. Clicking **Fetch from
  printer** raises a persistent "Fetching raw footage…" toast that stays up
  for the whole (often multi-minute) harvest and is dismissed automatically
  when it finishes (or the user can close it early).
- A **Disk used** total, and while a harvest runs a determinate **chunk-count
  progress bar** (`done/total`) with the **live download speed** of the chunk
  currently transferring beside it, plus a **Stop** button (admin-only,
  `cancel_harvest`) that aborts the pull mid-transfer — chunks already
  downloaded are kept and the group is left `incomplete` for a re-harvest.
- A group table (preview, **Print Job**, duration, resolution, size, status)
  with a preset picker + **Concat + Render** / **Discard** per group, and an
  expandable per-chunk list. Each present chunk has a **delete** button that
  permanently removes it and its slot-pair siblings (admin-only, confirmed).
  **Discard** (group-level, available whether or not the group was rendered)
  permanently deletes the whole group from disk; the per-chunk **delete**
  permanently removes a single chunk plus its slot-pair siblings — different
  scope, both immediate.
- A **render queue** table when jobs are active, with columns **Print Job** /
  Preset / State / Progress. Preset/State/Progress are given fixed narrow
  widths (their longest values are known) so the Print Job column takes the
  remaining slack; the preset shows its human label (e.g. "Quality 1080p").

Lazily scans the library the first time the subtab is shown, and reconciles the
pipeline state via `pipeline_status` on tab open (reload-safe).

## ViewModel dependencies

```js
OCTOPRINT_VIEWMODELS.push({
  construct: BambucamViewModel,
  dependencies: [
    "settingsViewModel",
    "loginStateViewModel",
    "printerStateViewModel",
  ],
  elements: [
    "#settings_plugin_bambucam",
    "#bambucam_webcam_container",
    "#tab_plugin_bambucam",
  ],
});
```

`printerStateViewModel` gates move/delete in the UI while a print is running.

See the [JavaScript API](../api/javascript.md) for the generated method
reference.
