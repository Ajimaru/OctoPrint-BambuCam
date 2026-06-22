# Python API

Automatically generated from the source docstrings with
[mkdocstrings](https://mkdocstrings.github.io/). Run `mkdocs serve` to render
this page locally.

## Plugin

The OctoPrint plugin implementation: mixins, settings, webcam provider and the
simple API.

::: octoprint_bambucam.BambucamPlugin
options:
show_root_heading: true
members_order: source

## Daemon supervisor

The supervised process manager for the vendored MJPEG daemon.

::: octoprint_bambucam.daemon.WebcamdManager
options:
show_root_heading: true
members_order: source

## Timelapse operations

Copy/move/delete batches and the `.avi` → `.mp4` transcode, mixed into the
plugin.

::: octoprint_bambucam.timelapse_ops.TimelapseOpsMixin
options:
show_root_heading: true
members_order: source

## Auto-sync

Pulls the new timelapse after a print, once the printer and OctoPrint are idle.

::: octoprint_bambucam.autosync.AutoSyncMixin
options:
show_root_heading: true
members_order: source

## FTPS client

Implicit-TLS FTPS client for the printer's SD-card timelapses.

::: octoprint_bambucam.ftp.BambuTimelapseFtp
options:
show_root_heading: true
members_order: source

## MQTT client

One-shot MQTT client for the printer's light control.

::: octoprint_bambucam.mqtt.BambuMqttClient
options:
show_root_heading: true
members_order: source

## MQTT monitor

Standing MQTT connection that tracks the printer's real light state and
publishes commands over the same socket. Used only as a fallback when
BambuConnector is not the active connection.

::: octoprint_bambucam.mqtt.BambuMqttMonitor
options:
show_root_heading: true
members_order: source

## Connector light path

Drives the light over OctoPrint-BambuConnector's already-open connection (its
`bpm.BambuPrinter.light_state`), avoiding a second socket to the printer's
slot-limited broker. The preferred path for light control + state.

::: octoprint_bambucam.connector_led
options:
show_root_heading: true
members_order: source
filters: ["!^_"]

## Transcoder

Wraps OctoPrint's ffmpeg for the `.avi` → `.mp4` re-encode.

::: octoprint_bambucam.transcode.TimelapseTranscoder
options:
show_root_heading: true
members_order: source

## Bambu Connector probe

Best-effort discovery of connection data from OctoPrint-BambuConnector.

::: octoprint_bambucam.bambu_connector.ConnectorInfo
options:
show_root_heading: true
members_order: source
