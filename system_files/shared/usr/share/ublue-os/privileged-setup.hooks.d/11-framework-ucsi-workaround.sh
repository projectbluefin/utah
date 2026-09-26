#!/usr/bin/bash

# shellcheck source=/dev/null
source /usr/lib/ublue/setup-services/libsetup.sh

# Compat shim: common libsetup.sh builds older than projectbluefin/common #1196
# stamp the version inside version-script itself and provide no
# version-script-commit. Define a no-op so this hook works against both the old
# (stamp-on-check) and the new (stamp-on-commit) contracts.
if ! declare -F version-script-commit >/dev/null; then
    version-script-commit() { :; }
fi

version-script framework-ucsi-workaround privileged 1 || exit 0

set -euo pipefail

VENDOR_PATH="/sys/devices/virtual/dmi/id/chassis_vendor"
PRODUCT_PATH="/sys/devices/virtual/dmi/id/product_name"
WORKAROUND_KARG="usbcore.autosuspend=-1"

if [[ ! -r "${VENDOR_PATH}" || ! -r "${PRODUCT_PATH}" ]]; then
    # Transient: DMI may become readable later, so do not commit; retry next boot.
    echo "Framework UCSI workaround skipped: DMI information not available."
    exit 0
fi

vendor="$(<"${VENDOR_PATH}")"
product_name="$(<"${PRODUCT_PATH}")"

if [[ "${vendor}" != "Framework" ]]; then
    # Deliberate skip: hardware will not change under a running system.
    version-script-commit framework-ucsi-workaround privileged 1
    exit 0
fi

if [[ ! "${product_name}" =~ Intel\ Core\ Ultra ]]; then
    # Deliberate skip: hardware will not change under a running system.
    version-script-commit framework-ucsi-workaround privileged 1
    exit 0
fi

if ! command -v rpm-ostree >/dev/null 2>&1; then
    # Transient: rpm-ostree may be present on a later boot, so do not commit.
    echo "Warning: rpm-ostree not found; unable to apply Framework UCSI workaround."
    exit 0
fi

if rpm-ostree kargs | grep -Fq "${WORKAROUND_KARG}"; then
    # Deliberate skip: the karg is already applied, nothing left to do.
    echo "Framework UCSI workaround already configured: ${WORKAROUND_KARG}"
    version-script-commit framework-ucsi-workaround privileged 1
    exit 0
fi

rpm-ostree kargs --append-if-missing="${WORKAROUND_KARG}"
echo "Applied Framework UCSI workaround (${WORKAROUND_KARG}). Reboot to activate."

# Record success only after the body ran, so a failing first-boot hook retries
# next boot instead of being permanently skipped (common #1196 new contract).
version-script-commit framework-ucsi-workaround privileged 1
