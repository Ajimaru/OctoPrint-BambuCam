# Security Policy

## Supported versions

| Version | Supported |
| ------- | --------- |
| 0.1.x   | ✅        |
| < 0.1   | ❌        |

## Reporting a vulnerability

**Please do not open a public GitHub issue for security vulnerabilities.**

Report vulnerabilities by email to
**[ajimaru_gdr@pm.me](mailto:ajimaru_gdr@pm.me)**. Include:

- A description of the vulnerability and its potential impact.
- Steps to reproduce or a proof-of-concept (if safe to share).
- Any suggested mitigations you are aware of.

You will receive an acknowledgement within **72 hours** and a status update
within **7 days**. If the issue is confirmed, a patch and coordinated disclosure
will follow as quickly as possible.

## Scope

Issues in scope for this project:

- Authentication or authorization bypasses in the plugin's API endpoints.
- Arbitrary file read/write via plugin settings or API.
- Unsafe shell command execution triggered by user-controlled input.
- Information disclosure (e.g. leaking the printer access code to unauthorized users).
- Denial of service caused by the plugin's daemon management logic.

Out of scope:

- Vulnerabilities in OctoPrint itself — please report those to the
  [OctoPrint project](https://github.com/OctoPrint/OctoPrint/security).
- Vulnerabilities in the vendored `webcamd-bambu` upstream code — please also
  report those upstream and CC the email above.
- Issues that require physical access to the OctoPrint host.
- The unauthenticated MJPEG stream when the user has explicitly configured
  `bind_address = 0.0.0.0` (this is a documented trade-off, not a bug).
