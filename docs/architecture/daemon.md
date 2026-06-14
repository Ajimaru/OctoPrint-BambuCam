# Daemon supervisor

`WebcamdManager` (`octoprint_bambucam/daemon.py`) runs the vendored
`webcam.py` as a supervised child process. See the
[Python API](../api/python.md) for the full method reference; this page
explains the design.

## Responsibilities

- **start / stop / restart** the child process.
- **Log pumping** — child `stdout`/`stderr` is forwarded into the plugin logger,
  with `--loghttp` request lines routed to a separate rotating HTTP log file.
- **Watchdog** — restarts the daemon after unexpected exits with exponential
  backoff, giving up after too many crashes in a window.

## Working directory

The child is launched with `cwd=VENDOR_DIR` so `webcam.py` can resolve its
bundled `SourceCodePro-Regular.ttf` watermark font via a relative path when
`--showfps` is enabled.

## Single-daemon guarantee

Two daemons must never run in parallel (they would fight over the same port). A
monotonic **generation** counter enforces this:

- `stop()` and `_spawn()` both bump `self._generation`.
- Each watchdog is tied to the generation of its spawn.
- A watchdog that notices `generation != self._generation` is _superseded_ and
  bows out immediately, leaving only the newest watchdog to act.

`start()` also calls `stop()` **outside the lock** first (since `stop()` blocks
on `process.wait()`), guaranteeing teardown of any previous instance before a
new one is spawned.

## Backoff & give-up policy

```text
unexpected exit
   ├─ autorestart == False ───────────────► stop
   ├─ crashes within restart_window ≤ max ─► sleep(backoff) ► respawn
   │        backoff: 2s → 4s → … capped at 60s
   │        (a healthy run > 60s resets backoff to 2s)
   └─ crashes within restart_window > max ─► "gave_up" ► notify, stop
```

Defaults: `max_restarts = 5`, `restart_window = 300 s`.

## State notifications

On `started`, `stopped`, `crashed`, and `gave_up`, the manager invokes the
`on_state_change(state, detail)` callback. The plugin forwards these to the
browser via `send_plugin_message`, where the frontend shows a `PNotify` on
`gave_up` and reloads the stream on `started`.

## Connection probe

`WebcamdManager.test_connection(hostname, access_code)` performs the Bambu LAN
camera handshake directly (TLS to port `6000`, the §2.2 auth frame) and returns
one of `ok`, `unreachable`, `auth_failed`, `timeout`, `error` — without
starting the daemon. This backs the settings dialog's **Test connection**
button.
