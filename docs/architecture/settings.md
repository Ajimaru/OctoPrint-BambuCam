# Settings

Settings defaults live in `BambucamPlugin.get_settings_defaults`. The full
reference (types, effects) is on the
[Configuration reference](../reference/configuration.md) page; this page covers
the **behaviour** around settings.

## Restart-on-change

A subset of keys requires the daemon to be restarted when changed. These are
listed in the module-level `DAEMON_SETTINGS` tuple:

```python
DAEMON_SETTINGS = (
    "enabled", "hostname", "access_code", "port", "bind_address",
    "width", "height", "rotate", "flashred", "showfps", "loghttp",
    "encodewait", "autorestart", "max_restarts", "restart_window",
)
```

`on_settings_save` snapshots these keys before and after the save. If any
changed:

- and the plugin is **enabled** → `WebcamdManager.restart(config)`.
- and the plugin is **disabled** → `WebcamdManager.stop()`.

`stream_url_override` is deliberately **not** in `DAEMON_SETTINGS`: it only
affects the URL the browser uses, so no daemon restart is needed.

## Restricted paths

```python
def get_settings_restricted_paths(self):
    return {"admin": [["access_code"]]}
```

The access code is **admin-restricted** — it is never sent to non-admin clients
in the settings tree.

## Config translation

`_daemon_config()` converts the stored settings into the typed dict consumed by
`WebcamdManager`, which `_build_argv()` then turns into `webcam.py` CLI flags.
