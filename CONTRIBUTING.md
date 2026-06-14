# Contributing to OctoPrint-BambuCam

Thank you for considering a contribution!
The following guidelines help keep the project
consistent and the review process smooth.

## Code of Conduct

All interactions in this project are governed by our
[Code of Conduct](CODE_OF_CONDUCT.md). Please read it before participating.

## What we welcome

- **Bug reports** — reproducible issues with a systeminfo bundle and log output.
- **Feature suggestions** — open an issue first to discuss before implementing.
- **Focused code changes** — small, targeted PRs are easier to review
  than large refactors.
- **Documentation improvements** — fixes, clarifications, and translations.

## Development setup

```bash
# 1. Clone and create a virtual environment
git clone https://github.com/Ajimaru/OctoPrint-BambuCam.git
cd OctoPrint-BambuCam
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate

# 2. Install in editable mode with dev dependencies
pip install -e ".[develop]"
pip install pytest pytest-cov pre-commit
npm install --no-save       # for ESLint / Prettier hooks

# 3. Install git hooks
pre-commit install
```

## Running tests

```bash
pytest tests/ -v
```

## Code style

This project uses [Ruff](https://docs.astral.sh/ruff/) for Python linting and formatting,
[ESLint](https://eslint.org/) and [Prettier](https://prettier.io/) for
JavaScript, and
[Bandit](https://bandit.readthedocs.io/) for security scanning. All checks run
automatically via pre-commit.

Run all checks manually:

```bash
pre-commit run --all-files
```

## Pull request standards

- One logical change per PR — avoid mixing features with refactors.
- All new behaviour must be covered by tests.
- Update the relevant documentation (README, docstrings).
- Add a `CHANGELOG.md` entry under the _Unreleased_ section.
- Write clear commit messages (imperative, ≤ 72 chars subject line).

## Licensing

By contributing, you agree that your contribution will be licensed under the
[AGPL-3.0-or-later](LICENSE) license that covers this project.
