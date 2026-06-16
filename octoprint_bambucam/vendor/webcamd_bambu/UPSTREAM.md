# Vendored: webcamd-bambu

- **Upstream repository:** <https://github.com/disconn3ct/webcamd-bambu>
- **Branch:** `bambu`
- **Pinned commit:** `befdccf3da58def143892d344caa9dde0823b770` (2024-03-24)
- **Vendored on:** 2026-06-12
- **License:** GPL-3.0 (see `LICENSE` in this directory)

## Files

| File                        | Origin                                                 | SHA-256 (as downloaded, before local patches)                      |
| --------------------------- | ------------------------------------------------------ | ------------------------------------------------------------------ |
| `webcam.py`                 | upstream verbatim + local patches below                | `34c80fab6aad78c582ab0e5859ab8c4164f8cec1387340c6a361b7c1e793cac6` |
| `LICENSE`                   | upstream verbatim (GPL-3.0)                            | `3972dc9744f6499f0f9b2dbf76696f2ae7ad8af9b23dde66d6af86c9dfb36986` |
| `SourceCodePro-Regular.ttf` | upstream verbatim (`--showfps` watermark; see patch 4) | `74bd80d3e42a08517cd7e1108ba3d86f2da29ac0f3065be95e0357956ab9db37` |

## Authors / credits

- Igor Maculan `<n3wtron@gmail.com>` — original author
- Christopher RYU `<software-github@disavowed.jp>` — fixes
- Shell Shrader `<shell@shellware.com>` — refactor & threading optimizations
- disconn3ct (SMS) — Bambu printer camera streaming (Jan 2024),
  repo maintainer

## Local patches (marked `OctoPrint-BambuCam patch` in the source)

1. `/?info` no longer includes the printer access code (`config.password` is
   redacted).
2. `/?shutdown` is disabled (returns 403). The endpoint had no authentication
   and the
   process lifecycle is managed by the OctoPrint plugin instead.
3. `ThreadingHTTPServer.allow_reuse_address = True` so a restart on an
   unchanged port does not fail with `EADDRINUSE` while the previous socket is
   still in `TIME_WAIT`.
4. The `--showfps` watermark font is loaded from an absolute path derived from
   `__file__` (`FONT_PATH`) instead of the bare filename, which resolved against
   the process working directory and raised `OSError: cannot open resource`.
5. The camera TLS context floors the protocol at TLS 1.2
   (`ctx.minimum_version = ssl.TLSVersion.TLSv1_2`) so legacy TLSv1/1.1 are
   never negotiated. Closes CodeQL `py/insecure-protocol`. Cert/hostname
   verification stays disabled (printer uses a self-signed cert + custom
   handshake).

No other modifications. When updating to a newer upstream commit, re-apply all
patches and update the pinned commit, date, and hashes above.
