#!/usr/bin/env bash

# If running on native Windows, re-exec this script under Git Bash if available.
# This is idempotent: if already running under Bash it does nothing.
if cd "$(dirname "$0")" >/dev/null 2>&1; then _SCRIPT_DIR_HINT="$(pwd)"; else _SCRIPT_DIR_HINT="$(dirname "$0")"; fi
if [ -z "${REPO_ROOT-}" ]; then
    # Prefer the git top-level so file paths passed by pre-commit resolve,
    # whether this runs from .githooks or .development/.githooks.
    if ! REPO_ROOT="$(git -C "$_SCRIPT_DIR_HINT" rev-parse --show-toplevel 2>/dev/null)"; then
        REPO_ROOT="$(cd "$_SCRIPT_DIR_HINT/.." >/dev/null 2>&1 && pwd || echo "$_SCRIPT_DIR_HINT")"
    fi
fi
# Primary wrapper: .development/win-bash-wrapper.sh (toolkit location).
# Fallback: scripts/win-bash-wrapper.sh (legacy / standalone repo layout).
WRAPPER="$REPO_ROOT/.development/win-bash-wrapper.sh"
if [ ! -x "$WRAPPER" ]; then
    WRAPPER="$REPO_ROOT/scripts/win-bash-wrapper.sh"
fi
if [ -z "${BASH_VERSION-}" ]; then
    if [ -x "$WRAPPER" ]; then
        exec "$WRAPPER" "$0" "$@"
    elif command -v bash >/dev/null 2>&1; then
        exec bash "$0" "$@"
    else
        echo "ERROR: bash is required to run prettier-hook.sh" >&2
        exit 127
    fi
fi

# Description: Format files using Prettier for pre-commit hooks.
# Behavior:
#  - Uses local `node_modules/.bin/prettier --write` when available, otherwise falls back to `npx --yes prettier --write`.
#  - Intended to be invoked from git pre-commit hooks with a list of files to format.
# Usage:
#  - .githooks/prettier-hook.sh <file>...

set -euo pipefail

usage() {
    cat <<'USAGE'
Usage: .githooks/prettier-hook.sh <file>...

Format files using Prettier. Uses local `node_modules/.bin/prettier` when available,
otherwise falls back to `npx --yes prettier --write`.

Options:
    -h, --help    Show this help message and exit
USAGE
}

if [[ "${1:-}" = "-h" || "${1:-}" = "--help" || "${1:-}" = "help" ]]; then
    usage
    exit 0
fi

# Prettier pre-commit hook helper (moved to .githooks)
# Usage: prettier-hook.sh <file>...

cd "$REPO_ROOT"

# Ensure at least one file argument was provided; avoid invoking Prettier with no targets.
if [ "$#" -eq 0 ]; then
    echo "ERROR: no files provided to prettier-hook.sh" >&2
    usage
    exit 2
fi

if [[ -x "${REPO_ROOT}/node_modules/.bin/prettier" ]]; then
    exec "${REPO_ROOT}/node_modules/.bin/prettier" --write "$@"
else
    # Try to install local node dependencies if we have a package.json (helps pre-commit.ci)
    if command -v node >/dev/null 2>&1 && [ -f package.json ]; then
        echo "Installing npm dependencies (prettier) if missing..."
        # try npm ci with a few retries to mitigate transient network errors
        attempt=0
        npm_ci_succeeded=false
        until [ "$attempt" -ge 3 ]; do
            if npm ci --no-audit --no-fund; then
                npm_ci_succeeded=true
                break
            fi
            attempt=$((attempt + 1))
            sleep $((2 ** attempt))
        done
        if [ "$npm_ci_succeeded" != "true" ]; then
            echo "Warning: 'npm ci' failed after $attempt attempts; dependencies may not be installed. Falling back to 'npx' for Prettier." >&2
        fi
        if [ -x "${REPO_ROOT}/node_modules/.bin/prettier" ]; then
            exec "${REPO_ROOT}/node_modules/.bin/prettier" --write "$@"
        fi
    fi

    # Use npx fallback with retries; --yes to avoid prompts
    attempt=0
    until [ "$attempt" -ge 3 ]; do
        if npx --yes prettier --write "$@"; then
            exit 0
        fi
        attempt=$((attempt + 1))
        echo "npx/prettier attempt $attempt failed, retrying..."
        sleep $((2 ** attempt))
    done
    # final attempt to show the error
    exec npx --yes prettier --write "$@"
fi
