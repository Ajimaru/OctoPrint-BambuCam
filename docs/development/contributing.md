# Contributing

The canonical guide is
[CONTRIBUTING.md](https://github.com/Ajimaru/OctoPrint-BambuCam/blob/main/CONTRIBUTING.md)
in the repo. The essentials:

## Setup

```bash
git clone https://github.com/Ajimaru/OctoPrint-BambuCam.git
cd OctoPrint-BambuCam
python -m venv .venv && source .venv/bin/activate
pip install -e ".[develop]"
pip install pytest pytest-cov pre-commit
npm install --no-save
pre-commit install
```

## Code style

- **Python**: [Ruff](https://docs.astral.sh/ruff/) (lint + format), line length
  **80**.
- **JavaScript**: [ESLint](https://eslint.org/) + [Prettier](https://prettier.io/).
- **Security**: [Bandit](https://bandit.readthedocs.io/).

All run automatically via pre-commit:

```bash
pre-commit run --all-files
```

## Pull request standards

- One logical change per PR — don't mix features with refactors.
- New behaviour must be covered by tests.
- Update the relevant docs (README, docstrings, this site).
- Add a `CHANGELOG.md` entry under _Unreleased_.
- Imperative commit subjects, ≤ 72 chars.

## Code of Conduct

All interactions follow the
[Code of Conduct](https://github.com/Ajimaru/OctoPrint-BambuCam/blob/main/CODE_OF_CONDUCT.md).

## Licensing

Contributions are licensed under **AGPL-3.0-or-later**.
