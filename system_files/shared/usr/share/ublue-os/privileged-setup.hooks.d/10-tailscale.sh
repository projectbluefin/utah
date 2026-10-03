#!/usr/bin/bash

# shellcheck source=/dev/null
source /usr/lib/ublue/setup-services/libsetup.sh

# Older common images retain the legacy stamp-on-check helper. Use the
# read-only pair when available, without requiring a common image bump first.
if ! declare -F version-script-check >/dev/null; then
    version-script-check() { version-script "$@"; }
    version-script-commit() { :; }
fi

set -xeuo pipefail

# Tailscale is optional on Utah. If the binary is absent (a minimal build, or
# the package was never selected) defer setup instead of failing the first-boot
# hook: stamping a version here would mark the hook done before it ever ran, so
# a later tailscale install would never complete the operator grant. Record the
# deferred state in the log so users and CI can see setup is pending, and retry
# on the next boot once the binary exists.
if ! command -v tailscale >/dev/null 2>&1; then
    echo "tailscale is not installed; deferring privileged operator setup until it is present"
    exit 0
fi

# Only a non-root pkexec caller can be the operator. Resolve the account before
# checking the version so invalid/transient callers never burn a legacy stamp.
if [[ ! "${PKEXEC_UID:-}" =~ ^[0-9]+$ ]] || [[ "${PKEXEC_UID}" =~ ^0+$ ]] \
    || ! getent passwd "${PKEXEC_UID}" >/dev/null 2>&1; then
    echo "no usable calling UID; deferring tailscale privileged setup until run under pkexec"
    exit 0
fi
operator="$(getent passwd "${PKEXEC_UID}" | cut -d: -f1)"
if [[ -z "${operator}" || "${operator}" == root ]]; then
    echo "no non-root calling account; deferring tailscale privileged setup"
    exit 0
fi

version-script-check tailscale privileged 1 || exit 0

tailscale set --operator="${operator}"
version-script-commit tailscale privileged 1
