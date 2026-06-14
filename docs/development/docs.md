# Building the docs

This site is built with [MkDocs](https://www.mkdocs.org/) +
[Material](https://squidfunk.github.io/mkdocs-material/). The Python API pages
are auto-generated from docstrings with
[mkdocstrings](https://mkdocstrings.github.io/); the JavaScript API page is
generated from JSDoc comments with
[jsdoc-to-markdown](https://github.com/jsdoc2md/jsdoc-to-markdown).

## Install

```bash
pip install -r requirements-docs.txt
```

## Live preview

```bash
mkdocs serve
```

Open <http://127.0.0.1:8000>. The Python API pages render live from the source
docstrings.

## Regenerate the JavaScript API page

`docs/api/javascript.md` is **generated, not hand-edited**. To refresh it after
changing JSDoc comments:

```bash
npm install --save-dev jsdoc-to-markdown   # first time only
./scripts/generate-jsdocs.sh
```

The script scans `octoprint_bambucam/static/js/**/*.js`, uses
`docs/jsdoc.json` as the jsdoc config, and writes the Markdown output.

## Build the static site

```bash
mkdocs build --strict
```

Output goes to `./site`. `--strict` turns warnings (e.g. broken links) into
errors — the same as CI.

## Deployment

The `docs` workflow (`.github/workflows/docs.yml`) regenerates the JS API page,
builds the site in strict mode, and deploys it to GitHub Pages on every push to
`main`.
