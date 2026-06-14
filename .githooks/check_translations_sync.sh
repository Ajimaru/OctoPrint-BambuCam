#!/usr/bin/env bash

# Pre-commit hook: keep translation catalogs in sync, automatically.
#
# Behavior:
#  - Runs .development/compile_translations.sh, which re-extracts the POT from
#    the current sources, updates the PO files (with --no-fuzzy-matching),
#    compiles the MO files, copies them into the plugin package, and verifies
#    the catalogs (fails on stray fuzzy entries or an 'en' msgstr that differs
#    from its msgid).
#  - Compares the regenerated catalogs against the staged/working versions
#    using a NORMALIZED comparison (volatile POT-Creation-Date / Generated-By
#    headers and shifting "#: file:line" references are ignored). Only a real
#    change in msgids/msgstrs counts.
#  - On a real content change, the hook re-stages the changed translation
#    files and FAILS once (standard pre-commit auto-fixer convention, like
#    black/prettier): the fix is staged for you, just re-run `git commit`.
#  - If only volatile metadata changed, the working tree is restored to avoid
#    noisy timestamp-only diffs, and the commit proceeds.
#  - If the compile/verify step itself fails, the hook fails with that error.
#
# This catches the case where a formatter (djlint/prettier) reflows templates:
# msgid text is unchanged but POT line references move, so the catalogs must be
# regenerated to stay technically in sync without producing churn.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if ! REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel 2>/dev/null)"; then
    REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
fi
cd "$REPO_ROOT"

COMPILE_SCRIPT="$REPO_ROOT/.development/compile_translations.sh"
if [[ ! -f "$COMPILE_SCRIPT" ]]; then
    echo "ERROR: $COMPILE_SCRIPT not found." >&2
    exit 1
fi

VENV_PYTHON="$REPO_ROOT/.venv/bin/python3"

# Translation artifacts that this hook may regenerate and re-stage.
TRANSLATION_PATHS=(
    "translations"
    "octoprint_bambucam/translations"
)

# Snapshot current PO/POT content (normalized) before regenerating, so we can
# tell a real change from pure timestamp churn.
snapshot_normalized() {
    if [[ ! -x "$VENV_PYTHON" ]]; then
        # No venv python: fall back to "changed if git says so".
        git status --porcelain -- "${TRANSLATION_PATHS[@]}" 2>/dev/null || true
        return
    fi
    "$VENV_PYTHON" - <<'PY'
import hashlib
import sys
from pathlib import Path

try:
    import polib
except ImportError:
    sys.exit(0)  # caller treats empty output as "unknown"; git diff decides

roots = ["translations", "octoprint_bambucam/translations"]
out = []
for root in roots:
    base = Path(root)
    if not base.exists():
        continue
    for f in sorted(base.rglob("*")):
        if f.suffix not in {".po", ".pot"} or not f.is_file():
            continue
        try:
            po = polib.pofile(str(f))
        except Exception:
            continue
        # Canonical content = sorted (msgid, msgctxt, msgstr) tuples only.
        # Ignores headers, line refs, flags ordering, file order.
        items = sorted(
            (e.msgctxt or "", e.msgid, e.msgstr)
            for e in po
            if not e.obsolete
        )
        h = hashlib.sha256(repr(items).encode("utf-8")).hexdigest()
        out.append(f"{f}:{h}")
print("\n".join(out))
PY
}

before="$(snapshot_normalized)"

echo "Regenerating translation catalogs (extract + update + compile + verify)..."
if ! FORCE_CLEAN=true bash "$COMPILE_SCRIPT"; then
    echo "ERROR: translation compilation/verification failed (see output above)." >&2
    echo "Fix the reported catalog problems, then re-run the commit." >&2
    exit 1
fi

after="$(snapshot_normalized)"

if [[ -n "$before" && "$before" == "$after" ]]; then
    # Only volatile metadata (timestamps / line refs) changed. Restore the
    # working tree for these paths to avoid noisy diffs, then proceed.
    echo "Translations already in sync (no msgid/msgstr changes)."
    git checkout -- "${TRANSLATION_PATHS[@]}" 2>/dev/null || true
    exit 0
fi

# Real content change (or no usable snapshot): re-stage and ask for re-commit.
echo "Translation catalogs were regenerated with content changes. Re-staging:" >&2
existing_paths=()
for path in "${TRANSLATION_PATHS[@]}"; do
    [[ -e "$path" ]] && existing_paths+=("$path")
done
if [[ ${#existing_paths[@]} -gt 0 ]]; then
    git add -- "${existing_paths[@]}"
    git status --porcelain -- "${existing_paths[@]}" | sed 's/^/  /' >&2
fi

cat >&2 <<'MSG'

Translation catalogs were out of sync and have been regenerated and staged.
This is expected after editing translatable strings or reformatting templates.
Please re-run your commit to include the updated catalogs.
MSG
exit 1
