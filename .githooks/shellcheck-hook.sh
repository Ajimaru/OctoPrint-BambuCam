#!/usr/bin/env bash

# If running on native Windows, re-exec this script under Git Bash if available.
# This is idempotent: if already running under Bash it does nothing.
ORIG_DIR="$(pwd)"
if cd "$(dirname "$0")" >/dev/null 2>&1; then _SCRIPT_DIR_HINT="$(pwd)"; else _SCRIPT_DIR_HINT="$(dirname "$0")"; fi
if [ -z "${REPO_ROOT-}" ]; then
    # Prefer git top-level so the Windows wrapper path resolves whether this
    # runs from .githooks or .development/.githooks.
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
        cd "$ORIG_DIR"
        exec "$WRAPPER" "$0" "$@"
    elif command -v bash >/dev/null 2>&1; then
        cd "$ORIG_DIR"
        exec bash "$0" "$@"
    else
        echo "ERROR: bash is required to run shellcheck-hook.sh" >&2
        exit 127
    fi
fi

cd "$ORIG_DIR"

# Description: Linting Shell scripts using ShellCheck for pre-commit hooks.
# Behavior:
#  - Checks for local shellcheck binary and validates version >= 0.11.0
#  - Emits a clear error if shellcheck is missing or outdated
#  - Intended to be invoked from git pre-commit hooks with a list of shell files to lint
# Usage:
#  - .githooks/shellcheck-hook.sh <file>...

set -euo pipefail

# Central pin lives in .development/versions.env; fall back to a literal so
# the hook keeps working even if that file is absent. This hook runs from two
# locations: the devkit source (.development/.githooks, where ../versions.env
# resolves) and the copied repo hooks (.githooks at the repo root, where the
# file lives one level deeper in .development/). Probe both.
_HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" >/dev/null 2>&1 && pwd)"
for _candidate in \
    "$_HOOK_DIR/../versions.env" \
    "$_HOOK_DIR/../.development/versions.env"; do
    if [[ -f "$_candidate" ]]; then
        set -a
        # shellcheck source=/dev/null
        . "$_candidate"
        set +a
        break
    fi
done
SHELLCHECK_MIN_VERSION="${SHELLCHECK_MIN_VERSION:-0.11.0}"

usage() {
    cat <<'USAGE'
Usage: .githooks/shellcheck-hook.sh <file>...

Lint shell scripts using ShellCheck. Requires ShellCheck >= 0.11.0.

Options:
    -h, --help    Show this help message and exit
USAGE
}

if [[ "${1:-}" = "-h" || "${1:-}" = "--help" || "${1:-}" = "help" ]]; then
    usage
    exit 0
fi

# Check if shellcheck is available
if ! command -v shellcheck >/dev/null 2>&1; then
    echo "ERROR: shellcheck is not installed or not in PATH" >&2
    echo "Please install ShellCheck >= $SHELLCHECK_MIN_VERSION" >&2
    echo "  Ubuntu/Debian: sudo apt-get install shellcheck" >&2
    echo "  macOS: brew install shellcheck" >&2
    echo "  See: https://github.com/koalaman/shellcheck?tab=readme-ov-file#installing" >&2
    exit 127
fi

# Check shellcheck version
SHELLCHECK_VERSION=$(shellcheck --version | sed -n 's/^version: \([0-9]*\.[0-9]*\.[0-9]*\).*/\1/p')
if [ -z "$SHELLCHECK_VERSION" ]; then
    echo "ERROR: Could not determine shellcheck version" >&2
    exit 1
fi

# Compare versions: convert X.Y.Z to integer for comparison (e.g., 0.11.0 -> 001100)
version_to_int() {
    local version="$1"
    local major=0 minor=0 patch=0
    IFS='.' read -r major minor patch <<< "$version"
    printf "%03d%03d%03d" "$major" "$minor" "$patch"
}

VERSION_INT="$(version_to_int "$SHELLCHECK_VERSION")"
MIN_VERSION_INT="$(version_to_int "$SHELLCHECK_MIN_VERSION")"

if [ "$VERSION_INT" -lt "$MIN_VERSION_INT" ]; then
    echo "ERROR: ShellCheck version $SHELLCHECK_VERSION is outdated" >&2
    echo "Minimum required version: $SHELLCHECK_MIN_VERSION" >&2
    echo "Please upgrade ShellCheck:" >&2
    echo "  Ubuntu/Debian: sudo apt-get install --only-upgrade shellcheck" >&2
    echo "  macOS: brew upgrade shellcheck" >&2
    echo "  See: https://github.com/koalaman/shellcheck?tab=readme-ov-file#installing" >&2
    exit 1
fi

# Ensure at least one file argument was provided
if [ "$#" -eq 0 ]; then
    echo "ERROR: no files provided to shellcheck-hook.sh" >&2
    usage
    exit 2
fi

# Run shellcheck with standard arguments
shellcheck --severity=warning "$@"
