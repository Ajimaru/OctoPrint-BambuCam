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
  backoff, giving up after too many crashes in a window. A printer that is
  **offline** (powered off / unreachable) is treated as an expected state, not a
  crash — see [Printer-offline handling](#printer-offline-handling).

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
process exit
   ├─ exit code 75 (printer offline) ──────► "offline" ► reconnect after 30s
   │        (does NOT count against max_restarts, never gives up)
   ├─ autorestart == False ───────────────► stop
   ├─ crashes within restart_window ≤ max ─► sleep(backoff) ► respawn
   │        backoff: 2s → 4s → … capped at 60s
   │        (a healthy run > 60s resets backoff to 2s)
   └─ crashes within restart_window > max ─► "gave_up" ► notify, stop
```

Defaults: `max_restarts = 5`, `restart_window = 300 s`.

## Printer-offline handling

A printer that is powered off or otherwise unreachable is a normal, expected
condition — not a crash — so it is handled separately from the backoff/give-up
policy above.

`webcam.py` distinguishes the two cases by **exit code**:

| Exit code          | Meaning                       | Watchdog reaction                    |
| ------------------ | ----------------------------- | ------------------------------------ |
| `75` (EX_TEMPFAIL) | Printer unreachable (offline) | Reconnect at a fixed 30 s interval   |
| `70` (EX_SOFTWARE) | Any other unexpected failure  | Normal crash path (backoff, give-up) |

When the printer is unreachable, `webcam.py` first retries a few times
internally (absorbing brief outages and reboots) while serving a **"Printer
Offline"** placeholder frame on the MJPEG stream — so the browser and any
`ffmpeg` consumer keep receiving a valid image instead of a frozen last frame or
a torn-down stream. If the printer stays unreachable it exits with `75`.

The watchdog then treats `75` specially: it emits an `offline` state and
respawns after a calm fixed interval (30 s) **without** counting the exit
against `max_restarts` and **without** ever giving up. The stream therefore
recovers automatically once the printer comes back online. Setting
`autorestart = False` disables this reconnect as well.

## State notifications

On `started`, `stopped`, `crashed`, `offline`, and `gave_up`, the manager
invokes the `on_state_change(state, detail)` callback. The plugin forwards these
to the browser via `send_plugin_message`, where the frontend reloads the stream
on `started`, shows an error `PNotify` on `gave_up`, and an informational
`PNotify` on `offline` (since an offline printer is expected and recovers on its
own).

## Connection probe

`WebcamdManager.test_connection(hostname, access_code)` performs the Bambu LAN
camera handshake directly (TLS to port `6000`, the §2.2 auth frame) and returns
one of `ok`, `unreachable`, `auth_failed`, `timeout`, `error` — without
starting the daemon. This backs the settings dialog's **Test connection**
button.
