# UI placements

The frontend viewmodel (`BambuCam.js`) binds three OctoPrint UI surfaces.

## Settings dialog

Template: `bambucam_settings.jinja2`, bound to `#settings_plugin_bambucam`.
Split into four tabs:

- **Connection** — a Configuration dropdown (`Manual` / `Auto` from Bambu
  Connector; `Auto` is disabled when the connector is unavailable), printer IP /
  hostname and access code (read-only in auto mode), and a **Test connection**
  button (`testConnection`).
- **Image** — resolution override, rotation, FPS watermark and activity-dot
  overlays.
- **Timelapse** — the `.avi` → `.mp4` conversion toggle with an **ffmpeg
  indicator** (`fetchFfmpegStatus`), plus the auto-sync controls.
- **Advanced** — daemon (port / bind address / restart policy) and diagnostics:
  the **Fetch info** button (`fetchInfo`) and **Restart** (`restartDaemon`),
  plus copyable snapshot/stream URLs.

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

## Timelapse tab

Template: `bambucam_tab.jinja2`, bound to `#tab_plugin_bambucam`.

The SD-card timelapse manager: a list (preview thumbnail, details, status) with
multi-select and a sort menu, **Copy / Move / Delete** batch actions with live
progress, and a **"Local .avi files"** section for converting leftovers. Lazily
loads the list the first time the tab is shown (`onTabChange`).

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
