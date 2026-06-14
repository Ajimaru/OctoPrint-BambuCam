# UI placements

The frontend viewmodel (`BambuCam.js`) binds two OctoPrint UI surfaces.

## Settings dialog

Template: `bambucam_settings.jinja2`, bound to `#settings_plugin_bambucam`.

Contains:

- **Connection fields** — printer IP / hostname and access code, plus a
  **Test connection** button (`testConnection`).
- **Status panel** — daemon running state, PID, encode FPS, session count, and
  last error. Polled every 10 s while the dialog is open
  (`onSettingsShown` / `onSettingsHidden`).
- **Diagnostics** — a **Fetch info** button (`fetchInfo`) that pretty-prints the
  daemon's `/?info` JSON (access code redacted).
- **Restart** button (`restartDaemon`).
- **Copyable URLs** — computed snapshot and stream URLs with copy-to-clipboard
  buttons.

## Webcam tab

Template: `bambucam_webcam.jinja2`, bound to `#bambucam_webcam_container`.

Renders the MJPEG `<img id="bambucam_stream">`. The viewmodel:

- computes the stream `src` (`streamUrl`), appending a cache-buster.
- shows a load/error state (`onStreamLoaded` / `onStreamErrored`).
- **retries** every 10 s on error (the daemon may still be starting).
- reloads the stream 2 s after settings are saved or the daemon reports
  `started`.

## ViewModel dependencies

```js
OCTOPRINT_VIEWMODELS.push({
  construct: BambucamViewModel,
  dependencies: ["settingsViewModel", "loginStateViewModel"],
  elements: ["#settings_plugin_bambucam", "#bambucam_webcam_container"],
});
```

See the [JavaScript API](../api/javascript.md) for the generated method
reference.
