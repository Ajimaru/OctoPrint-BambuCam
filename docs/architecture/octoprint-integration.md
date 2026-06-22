# OctoPrint integration

How the plugin plugs into OctoPrint's extension points.

## Webcam provider

`get_webcam_configurations()` returns a single `Webcam` object named
`bambucam`:

- `canSnapshot=True`, `snapshotDisplay` set to the loopback snapshot URL.
- `compat` (`WebcamCompatibility`) carries the stream URL, a `16:9` ratio, and
  the snapshot URL for legacy consumers.
- `extras` exposes the raw `stream` URL and `port`.

`take_webcam_snapshot(webcamName)` is a generator that streams the JPEG bytes
from `http://127.0.0.1:<port>/?snapshot`. It raises
`WebcamNotAbleToTakeSnapshotException` if the daemon is not running.

## Simple API

`get_api_commands()` declares the daemon, timelapse, connector and light
commands. `is_api_protected()` returns `True` and authorization is enforced per
command — **SETTINGS** for reads, **ADMIN** for SD-card writes and daemon
control, **CONTROL** for the light toggle. See the
[HTTP API reference](../reference/http-api.md) for the full command list,
permissions and request/response shapes.

Network-bound commands run on a background thread with a request cap
(`test_connection` ~12 s, `list_timelapses` ~30 s, `set_led` ~20 s); batch
copy/move/convert return immediately and report progress via push messages.

## Event handler

`on_event()` (via `EventHandlerPlugin`) tracks OctoPrint's print and
movie-render state. When a print ends, it triggers an after-print **auto-sync**
once the printer has stopped _and_ OctoPrint has finished rendering its own
timelapse — so two ffmpeg encodes never overlap. See
[Daemon supervisor](daemon.md) and the auto-sync flow in
[Data flow](data-flow.md).

It also records **real print dates**. On `PrintStarted` it snapshots the SD
card's videos (name → size); on `PrintDone` it waits for the printer to finish
rendering, finds the video that is new or grew since the snapshot, and stores
its real print-end time in the `print_dates` setting. This is the only reliable
date source for uncopied videos, because the A1 mini's camera clock is wrong in
LAN-only mode — see the date note in
[Configuration](../reference/configuration.md).

## Timelapse extensions

The `octoprint.timelapse.extensions` hook (`get_timelapse_extensions()`) adds
`avi` to OctoPrint's recognized timelapse extensions, so copied Bambu `.avi`
files show up in OctoPrint's native Timelapse tab alongside `.mp4`.

## Templates & assets

`get_template_configs()` registers a `settings` template
(`bambucam_settings.jinja2`), a `webcam` template (`bambucam_webcam.jinja2`,
with the live-stream light toggle) and a `tab` template
(`bambucam_tab.jinja2`, the SD-card timelapse manager). All use custom
bindings; `is_template_autoescaped()` returns `True`.

`get_assets()` bundles `js/BambuCam.js` and `css/BambuCam.css`.

## Software update hook

`get_update_information()` wires the `bambucam` plugin into OctoPrint's
software-update plugin as a `github_release` check against
`Ajimaru/OctoPrint-BambuCam`, registered through the
`octoprint.plugin.softwareupdate.check_config` hook.
