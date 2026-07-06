# Contributing to OctoPrint-BambuCam

Thanks for contributing! These guidelines keep the project consistent and
reviews smooth. All interactions are governed by our
[Code of Conduct](CODE_OF_CONDUCT.md).

## What we welcome

- **Bug reports** — reproducible issues with a systeminfo bundle and logs.
- **Feature suggestions** — open an issue first to discuss before implementing.
- **Documentation improvements** — fixes, clarifications, and translations.

## Development setup

```bash
git clone https://github.com/Ajimaru/OctoPrint-BambuCam.git
cd OctoPrint-BambuCam
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[develop]" pytest pytest-cov pre-commit
npm install --no-save        # for the ESLint / Prettier hooks
pre-commit install           # installs the git hooks
```

Run the tests with `pytest tests/ -v`.

## Code style

The git hooks (Black, isort, Ruff, flake8 and Bandit for Python; ESLint and
Prettier for JS/CSS) enforce the style on every commit — just commit and let
them apply any fixes. Don't hand-format to fight them.

## Pull request standards

- One logical change per PR — don't mix features with refactors.
- Cover new behaviour with tests and update the relevant docs.
- Keep the coverage gate green (`--cov-fail-under=90`).

## Branching and merging

`dev` is the integration branch; `main` is the protected, linear-history
release branch. **Open your PR from a short-lived feature branch into `dev`**.
Keep your branch rebased on `dev` (`git rebase origin/dev`)
rather than merging `dev` into it, so history stays linear.

## Licensing

By contributing, you agree your contribution is licensed under the
[AGPL-3.0-or-later](LICENSE) license that covers this project.
