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

`get_api_commands()` declares three commands:

| Command           | Required params           |
| ----------------- | ------------------------- |
| `restart`         | —                         |
| `test_connection` | `hostname`, `access_code` |
| `fetch_info`      | —                         |

`is_api_protected()` returns `True`. Authorization is enforced per command:
`fetch_info` and `on_api_get` need **SETTINGS**; `restart` and
`test_connection` need **ADMIN**. `test_connection` runs the probe on a
background thread and caps the request at 12 s.

## Templates & assets

`get_template_configs()` registers a `settings` template
(`bambucam_settings.jinja2`) and a `webcam` template
(`bambucam_webcam.jinja2`), both with custom bindings.
`is_template_autoescaped()` returns `True`.

`get_assets()` bundles `js/BambuCam.js` and `css/BambuCam.css`.

## Software update hook

`get_update_information()` wires the `bblp` plugin into OctoPrint's
software-update plugin as a `github_release` check against
`Ajimaru/OctoPrint-BambuCam`, registered through the
`octoprint.plugin.softwareupdate.check_config` hook.
