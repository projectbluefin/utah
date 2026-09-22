#!/usr/bin/bash

# shellcheck source=/dev/null
source /usr/lib/ublue/setup-services/libsetup.sh

# If Tailscale is not installed, defer configuration without failing.
# This keeps first boot clean and makes the deferred state explicit.
if ! command -v tailscale >/dev/null 2>&1; then
    echo "Tailscale binary not found; skipping Tailscale configuration (deferred)."
    exit 0
fi

# Operator configuration requires a target invoking user (e.g. from pkexec).
# If PKEXEC_UID is unset or cannot be resolved, defer operator configuration
# without stamping version 1 so it can run when an operator session is present.
OPERATOR_UID="${PKEXEC_UID:-}"
if [ -z "${OPERATOR_UID}" ]; then
    echo "PKEXEC_UID not set; skipping Tailscale operator configuration (deferred)."
    exit 0
fi

OPERATOR="$(getent passwd "${OPERATOR_UID}" | cut -d: -f1 || true)"
if [ -z "${OPERATOR}" ]; then
    echo "Operator user not resolved for UID ${OPERATOR_UID}; skipping Tailscale configuration (deferred)."
    exit 0
fi

version-script tailscale privileged 1 || exit 0

set -xeuo pipefail

if ! tailscale set --operator="${OPERATOR}"; then
    echo "Warning: tailscale set --operator failed (tailscaled daemon may not be active yet)."
    # Roll back the version stamp so a subsequent boot retries setup.
    # libsetup.sh keeps setup_versioning.json as jq-managed JSON, so only jq may
    # rewrite it: a line-oriented fallback would strip the "tailscale" key and
    # can leave a trailing comma, producing invalid JSON that breaks
    # version-script for every later hook. Leaving the stamp in place only
    # defers this one retry; corrupting the file breaks first boot entirely.
    checker="${SETUP_CHECKER_FILE:-${HOME}/.local/share/ublue/setup_versioning.json}"
    if [ -f "${checker}" ]; then
        if ! command -v jq >/dev/null 2>&1; then
            echo "Warning: jq is unavailable; leaving the Tailscale version stamp in ${checker} untouched."
        else
            tmp="$(mktemp)"
            if jq 'del(.version.privileged.tailscale)' "${checker}" > "${tmp}" 2>/dev/null; then
                mv "${tmp}" "${checker}"
                echo "Rolled back the Tailscale version stamp in ${checker}; setup will retry."
            else
                rm -f "${tmp}"
                echo "Warning: ${checker} is not valid JSON; leaving the Tailscale version stamp untouched."
            fi
        fi
    fi
    exit 0
fi
