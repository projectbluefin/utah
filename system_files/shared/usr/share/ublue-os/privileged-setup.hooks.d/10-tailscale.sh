#!/usr/bin/bash

# shellcheck source=/dev/null
source /usr/lib/ublue/setup-services/libsetup.sh

TAILSCALE_DEFERRED_STATE="${TAILSCALE_DEFERRED_STATE:-/var/lib/ublue/setup-services/tailscale.deferred}"

defer_tailscale() {
    local reason="$1"
    install -d -m0755 "$(dirname "${TAILSCALE_DEFERRED_STATE}")"
    {
        printf 'status=deferred\n'
        printf 'reason=%s\n' "${reason}"
        printf 'retry=rerun ublue-privileged-setup\n'
    } >"${TAILSCALE_DEFERRED_STATE}"
    echo "Tailscale setup deferred: ${reason}"
}

# Tailscale is an optional integration: this hook tolerates the tailscale
# package/unit being absent entirely and defers rather than failing. Do this
# check before version-script: recording a completed version while the
# package is absent would make the hook silently skip forever after
# Tailscale is installed.
if ! command -v tailscale >/dev/null 2>&1; then
    defer_tailscale "the tailscale binary is not installed"
    exit 0
fi

if [[ -z "${PKEXEC_UID:-}" ]]; then
    defer_tailscale "pkexec did not provide PKEXEC_UID"
    exit 0
fi

operator="$(getent passwd "${PKEXEC_UID}" | cut -d: -f1)"
if [[ -z "${operator}" ]]; then
    defer_tailscale "no passwd entry exists for PKEXEC_UID=${PKEXEC_UID}"
    exit 0
fi

# A deferred run must be retried even if an older version-script invocation
# recorded version 1 before the command became unavailable. A successful run
# removes the marker, after which version-script restores normal idempotency.
if [[ ! -e "${TAILSCALE_DEFERRED_STATE}" ]]; then
    version-script tailscale privileged 1 || exit 0
else
    echo "Retrying previously deferred Tailscale setup"
fi

set -xeuo pipefail

if ! tailscale set --operator="${operator}"; then
    defer_tailscale "tailscale set --operator failed"
    exit 0
fi

rm -f "${TAILSCALE_DEFERRED_STATE}"
