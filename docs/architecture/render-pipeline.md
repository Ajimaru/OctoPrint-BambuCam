# Raw Files render pipeline

Alongside the finished `/timelapse` videos, the A1 mini continuously records
**raw high-resolution timelapse chunks** into `/ipcam` (1680×1080 MJPEG `.avi`,
a 160-slot ring buffer ~24 h deep) — even when the on-screen timelapse was off.
The render pipeline harvests those chunks per print and lets the user concat +
render them into a finished `.mp4` that lands in OctoPrint's native Timelapse
tab.

The feature is exposed through the **Raw Files** subtab of the BambuCam tab and
its own **Raw Files Render** settings section.

## Components

| Module                                  | Role                                                             |
| --------------------------------------- | ---------------------------------------------------------------- |
| `octoprint_bambucam/ipcam_ftp.py`       | `/ipcam` listing + chunk download (subclass of the FTPS client). |
| `octoprint_bambucam/ipcam_sync.py`      | PrintStarted/Done snapshot-diff → auto-download (mixin).         |
| `octoprint_bambucam/render_paths.py`    | Storage layout, print-id builder, realpath containment.          |
| `octoprint_bambucam/raw_library.py`     | Footage registry: scans `raw/chunks/`, ffprobes, group state.    |
| `octoprint_bambucam/ffprobe.py`         | ffprobe JSON → duration/resolution/fps/codec/size.               |
| `octoprint_bambucam/render_registry.py` | Atomic `jobs.json` + cross-process lockfiles.                    |
| `octoprint_bambucam/render_queue.py`    | Single-worker queue, idle gate, cancel, startup recovery.        |
| `octoprint_bambucam/render_presets.py`  | The four scale/speed/crf presets.                                |
| `octoprint_bambucam/render_worker.py`   | ffmpeg concat (`-c copy`) + re-encode → timelapse folder.        |
| `octoprint_bambucam/raw_files_ops.py`   | API handlers + raw-preview thumbnails (mixin).                   |

## Data flow

```text
PrintStarted → /ipcam snapshot (baseline)
PrintDone    → record the print's real date/job on a side thread
               (SD poll → print_dates/print_jobs), plus
             → post-print pipeline (one serial worker, autosync.py):
   1.  wait auto_sync_delay + idle gate (ring-buffer write settles)
   2.  SD-card timelapse copy (when auto_sync)
   3.  /ipcam harvest (when auto_download_ipcam):
       → /ipcam snapshot-diff = this print's chunks
       → pause the live webcam stream (frees the printer's slow FTPS link)
       → download raw → raw/chunks/<print-id>/ + order.json
       → resume the webcam stream
       → saved nothing? discard the empty group dir (no phantom)
       → ffprobe + raw thumbnail → group state chunks_ready
       → auto_render_new_groups on? → render job queued automatically

User clicks "Concat + Render" (or auto-render queued it) → worker (idle only):
   1. ffmpeg -f concat -c copy   selected chunks → work/<jobid>.concat.avi
   2. ffmpeg re-encode (preset)                  → work/<jobid>.tmp.mp4
   3. os.replace (atomic)        → <octoprint>/timelapse/<name>.mp4
   4. ffmpeg -sseof -1           → <name>.mp4.thumb.jpg (last frame)
   5. fire MovieDone             → native Timelapse tab refreshes
   6. rendered.json marker written into raw/chunks/<id>/
   7. group stays listed as "Rendered" (re-render/discard possible)
```

Both the raw render (step 4 above) and the SD-timelapse transcode write a
sibling `<movie>.mp4.thumb.jpg` from the **last** frame (`ffmpeg -sseof -1`):
OctoPrint's native Timelapse list shows that file as the clip's thumbnail, and a
print timelapse's first frame is an empty bed (a blank white tile), whereas the
last frame shows the finished print. The thumbnail is best-effort — a failure
never fails the render/transcode, and it does not depend on OctoPrint's optional
`ffmpegThumbnailCommandline` (often unset on recent OctoPrint). When
`raw_thumb_from_gcode` is on, the print job's slicer plate preview (Bambu
Connector's `plate_1.png`) is used as the thumbnail instead, falling back to
the last video frame when no preview is found — see the
[gcode-preview thumbnails](../reference/configuration.md#raw-files-render-pipeline)
note.

The harvest is **stage 3 of one serial post-print worker**, not its own
thread, so it can never run concurrently with the SD-card copy (a second
FTPS session yields `425` on the printer). The print-date recorder polls the
SD card on its own side thread; its listing attempts simply fail-and-retry
while a transfer holds the printer's single FTPS slot. While the pipeline
runs it mirrors a `busy` flag and per-chunk progress to the UI (see the
`pipeline` push message), so the Timelapse tab holds its auto-refresh and the
Raw Files tab locks its buttons and shows a harvest progress bar.

The `<print-id>` is `<YYYY-MM-DD_HHMM>__<gcode-stem>` built from the **real**
PrintDone time (never the SD camera clock, which is wrong in LAN-only mode),
with a `-N` suffix on same-minute collisions.

The finished clip is named `<stem>__<date><suffix>_<preset>_RAW.mp4` (e.g.
`benchy__2026-07-04_1429_A1mini_original_RAW.mp4`): the print-id reordered to
job-first so raw renders sort next to the job-prefixed SD downloads,
`<suffix>` the optional `download_suffix`, and the trailing `_RAW` marking a
clip rendered from the raw `/ipcam` chunks rather than copied from the SD
card.

### Snapshot diff and the no-baseline safety cap

The chunk set for a print is the **diff** of two `/ipcam` snapshots: the
baseline taken at `PrintStarted` and the listing at `PrintDone` (a chunk that is
new, or whose size grew, belongs to this print). This is the same snapshot-diff
mechanism `autosync.py` uses for `print_dates`.

When **no baseline exists** — the `PrintStarted` snapshot failed, the print was
started from the SD card / touchscreen without OctoPrint seeing it, or the user
hit **Fetch from printer** manually — a naive diff against an empty set would
treat the **entire ~160-slot ring buffer (~17 GB)** as this print's footage and
try to download all of it. To prevent that, a no-baseline harvest is **capped to
the newest few chunks** (by FTP modify time, `_NO_BASELINE_MAX_CHUNKS`, default
6). A real but empty baseline (`/ipcam` genuinely empty at `PrintStarted`) still
does an exact diff. The cap is a safety floor, not a substitute for a real
baseline: for nameless SD prints the chunk-to-print mapping is best-effort. A
manual harvest with no PrintDone payload labels its group with the **most recent
recorded print job** (`print_jobs`) rather than the `unknown-print` fallback.

Listing/lifecycle hygiene rules apply throughout:

- `._name.avi` AppleDouble/resource-fork stubs the printer's FTP sometimes lists
  are filtered out (they are not footage).
- A leftover `*.part` from an interrupted download is cleared before a fresh
  harvest of the same group.
- A harvest that saves **no** chunks (all failed, or cancelled before the first
  byte) **removes the group directory** instead of leaving a 0-chunk phantom
  group the user has to clean up by hand.
- Overlapping **manual** harvests **queue** behind a lock and run back-to-back;
  a manual harvest no longer cancels an in-flight one (that was what stranded
  `.part` files and phantom groups). A running pipeline also disables the Raw
  Files buttons in the UI as a backstop.

### The printer's slow `/ipcam` link

The A1 mini serves `/ipcam` over FTPS at only **~180 KB/s** — measured live,
with _and_ without the webcam stream, so it is a firmware/link limit, not our
code (raw `curl` is just as slow). A single `/ipcam` chunk is ~135 MB, so one
chunk takes **~12 minutes** and a multi-chunk harvest runs for tens of minutes.
Two consequences shape the harvest:

- **Long data-channel timeout.** The FTPS _connect_ timeout stays at 20 s, but
  the _data socket_ is widened to `DOWNLOAD_TIMEOUT` (20 min per chunk) for the
  transfer only (`ftp.py`, `_download_timeout`). Without this a slow-but-healthy
  chunk would trip the 20 s socket timeout mid-transfer, and the whole harvest
  would save nothing.
- **Webcam pause.** The live MJPEG stream competes with the download for the
  printer's link, and a concurrent stream makes a multi-minute transfer more
  likely to stall. The harvest therefore stops `webcamd` for the duration of the
  pull and restarts it afterwards (`_webcam_paused_for_harvest`; best-effort —
  a restart failure never aborts the harvest). It buys ~10 % throughput plus
  stability.

Even so, a harvest is inherently slow (the printer serves `/ipcam` at roughly
180 KB/s); the UI shows a determinate chunk-count progress bar (`done/total`)
with the live download speed of the chunk currently transferring beside it, and,
on a partial failure, a specific toast naming the reason and how many chunks were
secured. The byte counter comes from the FTP transfer's per-block callback,
throttled to ~1 push/s server-side (`downloaded`/`download_total` on the
`pipeline` push); the displayed rate (`bytes_per_sec`) is derived server-side
from consecutive samples — so a 12-minute chunk visibly _lives_ rather than
looking frozen.

A running harvest can be **aborted from the UI**: the Stop button beside the
harvest bar (admin-only, `cancel_harvest` API) sets the harvest's cancel
event, which the per-block progress callback checks — so even a long chunk
transfer aborts promptly. Chunks already saved are kept and the group is left
`incomplete` (terminal push `reason: "cancelled"` with `got`/`want`), ready
for a re-harvest within the ring-buffer window. Both harvest paths register
their cancel event while running (`_ipcam_cancel`), so the button works for
the post-print pipeline stage as well as a manual fetch.

## Storage layout

Everything lives under the plugin data folder; the finished `.mp4` does **not**
— it goes straight to OctoPrint's timelapse folder.

```text
<plugin_data>/render/
  raw/chunks/<print-id>/   # raw downloaded /ipcam chunks + order.json
  work/                    # concat/tmp scratch (never shown in the UI)
  thumbs/                  # raw-preview thumbnails (last frame of last chunk)
  trash/                   # groups soft-deleted by the retention sweep
  metadata/jobs.json       # render job registry (atomic write)
```

The bulky directories — `raw/chunks/`, `work/`, `trash/` — are excluded from
OctoPrint backups via the `octoprint.plugin.backup.additional_excludes` hook:
chunks are re-harvestable from the printer's `/ipcam` folder and the rest is
transient. `thumbs/` and `metadata/` (small) stay in the backup so a restore
keeps thumbnails and the raw-library state consistent with the rendered
`.mp4`s in the timelapse folder. When the user excludes "timelapse" in the
backup dialog, the whole `render/` tree is dropped instead.

Two deletion paths exist. **Discarding a whole group** (`delete_group`, the
"Discard" button) removes the group directory from disk **permanently** — an
explicit user discard is immediate, not parked in `trash/`. **Deleting
individual chunks** (`delete_chunks`) likewise removes the files **permanently**,
pulling in each chunk's slot-pair siblings and rewriting `order.json` so the
remaining group stays consistent. An emptied group is forgotten. The `trash/`
directory is used only by the **retention sweep**, which soft-deletes expired
_rendered_ groups there when `move_to_trash` is on (and purges `trash/` later).

## Group and job states

- **Group** (`raw_library`): `incomplete` (download partial/failed) ·
  `chunks_ready` (downloaded + probed) · `rendered` (a `rendered.json` marker
  exists; the chunks stay on disk so the group remains listed and can be
  re-rendered with another preset or discarded manually).
- **Job** (`render_registry`): `queued` · `concat` · `render` · `done` ·
  `failed` · `cancelled`. `queued` is a queue state shown in the render-queue
  table, not a group badge. **Cancel** promptly interrupts a running job: the
  cancel event is polled inside the ffmpeg runner (on every stderr line) and
  the encode process is killed mid-flight rather than only being noticed after
  ffmpeg finishes — the partial `work/` output is removed and the job settles
  as `cancelled`. Long jobs are bounded by `render_timeout` (default 10800 s);
  exceeding it kills ffmpeg and fails the job.

## Presets

A preset (defined in `render_presets.py`) is a fixed bundle of four ffmpeg
encode parameters. The user picks one from the dropdown per group; there is no
raw-flag exposure.

| Preset          | Scale   | Speed | CRF | x264 preset |
| --------------- | ------- | ----- | --- | ----------- |
| `fast_720p`     | 1280:-2 | 5×    | 26  | veryfast    |
| `medium_1080p`  | 1920:-2 | 2×    | 23  | medium      |
| `quality_1080p` | 1920:-2 | 1×    | 20  | slow        |
| `original`      | 1680:-2 | 1×    | 18  | slow        |

What each column controls:

- **Scale** — target width with `-2` height (auto, even, aspect preserved),
  applied as `scale=<W>:-2`. `original` keeps the full `/ipcam` resolution (no
  meaningful downscale) for an archive of the raw quality — the slowest job on
  a Pi.
- **Speed** — the `N×` playback multiplier, realized as `setpts=(1/N)*PTS`, so
  a `5×` clip plays five times faster than real time (a long print → a short
  timelapse); `1×` keeps real-time pacing.
- **CRF** — H.264 quality/size knob; lower means better quality and a larger
  file.
- **x264 preset** — trades encode CPU time for compression efficiency
  (`veryfast` → quick but larger, `slow` → slower but smaller).

`fast_720p` is the default and the fallback for an unknown preset name.

The `-vf` filter is the combined `setpts=…,scale=…` string built by
`render_presets.build_vf`. The encode step also carries `-threads` from the
`ffmpeg_threads` setting (default 1), which caps CPU use so a background render
doesn't starve the Pi; the lossless concat step (`-c copy`) does no encoding and
is unaffected.

## Safety

The pipeline follows the same atomic/containment rules as the timelapse path:
ffmpeg is always an argv list (no shell); every derived path is realpath-checked
inside its base; the final `.mp4` is written to a `work/` temp then `os.replace`d
into place; `jobs.json` is written tmp + `os.replace` + `fsync`; lockfiles use
`O_CREAT|O_EXCL`; and startup recovery clears `work/`, reclaims stale locks, and
fails any job a crash left mid-render. Render runs only while the printer is
idle (default).
