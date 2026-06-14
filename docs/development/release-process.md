# Release process

Releases are published as **GitHub Releases**; OctoPrint's software-update hook
points users at `releases/latest`.

## Versioning

The project follows [SemVer](https://semver.org/). The version is derived at
build time (`octoprint_bambucam/_version.py`) and surfaced through
`get_update_information()` as `displayVersion`.

## Steps

1. Land all changes on `main` with green CI.
2. Move the `CHANGELOG.md` _Unreleased_ entries under the new version heading.
3. Tag the release:

   ```bash
   git tag v0.1.0
   git push origin v0.1.0
   ```

4. The `release` workflow (`.github/workflows/release.yml`) builds the wheel and
   attaches it to the GitHub Release.

## Update artifact

The software-update hook installs from:

```text
https://github.com/Ajimaru/OctoPrint-BambuCam/archive/{target_version}.zip
```

and the README points the Plugin Manager at:

```text
https://github.com/Ajimaru/OctoPrint-BambuCam/releases/latest/download/BambuCam-latest.whl
```

Make sure the release attaches a `BambuCam-latest.whl` asset so the
`releases/latest` install URL keeps working.
