#!/usr/bin/bash
# Per-vendor user setup for Framework laptops.
# Installs the Framework EC tool via Homebrew for hardware management.

# shellcheck source=/dev/null
source /usr/lib/ublue/setup-services/libsetup.sh

# Compat shim: common libsetup.sh builds older than projectbluefin/common #1196
# stamp the version inside version-script itself and provide no
# version-script-commit. Define a no-op so this hook works against both the old
# (stamp-on-check) and the new (stamp-on-commit) contracts.
if ! declare -F version-script-commit >/dev/null; then
    version-script-commit() { :; }
fi

version-script 20-framework user 1 || exit 0

set -euo pipefail

CHASSIS_VENDOR_PATH="/sys/devices/virtual/dmi/id/chassis_vendor"
BREW_PREFIX="/home/linuxbrew/.linuxbrew"

# Only run on Framework hardware. An unreadable DMI node is transient (it may be
# readable on a later boot), so skip without committing; a non-Framework vendor
# is a deliberate skip that will never change, so commit and stop re-running.
[[ -r "${CHASSIS_VENDOR_PATH}" ]] || exit 0
chassis_vendor="$(cat "${CHASSIS_VENDOR_PATH}")"
if [[ "${chassis_vendor}" != "Framework" ]]; then
    version-script-commit 20-framework user 1
    exit 0
fi

echo "Framework laptop detected — running Framework-specific user setup"

# Guard: brew must be available
if ! command -v brew >/dev/null 2>&1; then
    echo "Warning: brew not found — skipping Framework setup"
    exit 0
fi

# Guard: user must have write access to the Homebrew prefix
if [[ ! -w "${BREW_PREFIX}" ]]; then
    echo "Warning: user lacks write permission to ${BREW_PREFIX} — skipping Framework setup"
    exit 0
fi

# Install a package only when it is not already present
install_if_missing() {
    local pkg="$1"
    if brew list "${pkg}" >/dev/null 2>&1; then
        echo "${pkg} already installed, skipping"
    else
        echo "Installing ${pkg}..."
        brew install "${pkg}"
    fi
}

# Framework EC tool for hardware management (fan curves, battery charge limit, etc.)
install_if_missing "fw-ectool"

# Record success only after the body ran, so a failing first-boot hook retries
# next boot instead of being permanently skipped (common #1196 new contract).
version-script-commit 20-framework user 1
