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
   sdist and attaches them to the GitHub Release, plus a versioned source zip
   (`OctoPrint-BambuCam-<version>.zip`) and a stable
   `OctoPrint-BambuCam-latest.zip`.

## Update artifact

The software-update hook installs from:

```text
https://github.com/Ajimaru/OctoPrint-BambuCam/archive/{target_version}.zip
```

and the README points the Plugin Manager at:

```text
https://github.com/Ajimaru/OctoPrint-BambuCam/releases/latest/download/OctoPrint-BambuCam-latest.zip
```

The Plugin Manager installs a source **zip**, not a wheel — a wheel filename is
parsed by pip (`{name}-{version}-{pytag}-{abitag}-{platformtag}.whl`) and a
renamed/aliased wheel is rejected with "Invalid wheel filename". A zip has no
such constraint, so the workflow publishes `OctoPrint-BambuCam-latest.zip` to
keep the `releases/latest` install URL working. The wheel is still attached
under its original PEP 427 name for direct `pip install`.
