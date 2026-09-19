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

tailscale set --operator="${OPERATOR}" || {
    echo "Warning: tailscale set --operator failed (tailscaled daemon may not be active yet)."
}
