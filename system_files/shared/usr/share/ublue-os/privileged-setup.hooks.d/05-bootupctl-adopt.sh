#!/usr/bin/bash
# Adopt the on-disk bootloader into bootupd's managed set on first boot.
#
# `bootupd` records an installed version in /boot/bootupd-state.json; without
# that record, `bootupctl status` reports "No components installed" and no
# bootloader updates -- including shim updates that matter for Secure Boot
# switchers -- are ever offered or applied. The state is written when bootupd
# itself installs the components, not by `bootc install to-disk`, so a system
# switched into Utah (or fresh-installed from any image that did not run the
# adopt step) starts unmanaged even though every payload is already on disk.
#
# `bootupctl adopt-and-update` is the intended one-shot path: it adopts the
# current on-disk state as the managed version, then runs an update against
# the payloads the image ships. Idempotent on a system that is already
# managed -- the command is a no-op when nothing is adoptable.
#
# First-boot-only: common#1196 split `version-script` into a read-only check
# (`version-script-check`) and a commit (`version-script-commit`) so a failed
# first-boot hook retries next boot rather than being skipped forever after.
# The compat shim below lets this hook work under either contract in either
# merge order, matching 20-home-labels.sh.

# shellcheck source=/dev/null
source /usr/lib/ublue/setup-services/libsetup.sh

# Compat shim: common libsetup.sh builds older than projectbluefin/common#1196
# have only version-script, which records the version before the body runs, and
# no version-script-check/version-script-commit pair. Fall back to that legacy
# gate and make the commit a no-op, so this hook works against both contracts.
if ! declare -F version-script-check >/dev/null; then
    version-script-check() { version-script "$@"; }
    version-script-commit() { :; }
fi

set -xeuo pipefail

# Transient guards run before version-script-check: under the pre-1196 compat
# shim above the check is the legacy stamp, so a skip placed after it would
# be recorded as done and never retry. An exit without commit causes a retry
# next boot, which is the intended contract from common#1196 for transient
# skips.

# Skip cleanly when bootupctl is not installed (e.g. an image variant that
# does not carry it). Bootupctl may be present after a later bootc switch.
if ! command -v bootupctl >/dev/null 2>&1; then
    echo "bootupctl-adopt: bootupctl not installed; skipping"
    exit 0
fi

# A live session cannot adopt: `bootupctl adopt-and-update` requires a writable
# ESP and refuses to run on erofs/squashfs roots (its ExecCondition pattern).
# Re-trying next boot is cheap and correct; the live ISO does not need a
# managed bootloader.
if findmnt -n -o FSTYPE /sysroot 2>/dev/null | grep -Eq '^(erofs|squashfs)$'; then
    echo "bootupctl-adopt: live session root; skipping"
    exit 0
fi

# A systemd-boot layout has no GRUB second stage for bootupctl to adopt:
# `bootupctl adopt-and-update` unconditionally opens /boot/grub2 and fails
# when it is absent, leaving a failed transient bootupd.service behind. Utah
# boots systemd-boot (the GRUB binary on the ESP is QEMU fallback only), so
# there is nothing adoptable here. Skip without committing so a later bootc
# switch that provides a GRUB layout still retries (#741).
# BOOTUPCTL_BOOT_DIR exists so the host-side hook tests can point the guard
# at a scratch tree; production always uses /boot.
boot_dir="${BOOTUPCTL_BOOT_DIR:-/boot}"
if [ ! -d "$boot_dir/grub2" ]; then
    echo "bootupctl-adopt: no $boot_dir/grub2 (systemd-boot layout); skipping"
    exit 0
fi

version-script-check bootupctl-adopt privileged 1 || exit 0

# Adopt the on-disk bootloader and write /boot/bootupd-state.json. On a
# system already adopted the command is a no-op; failures (no EFI dir,
# missing payload) abort before the commit and retry next boot.
bootupctl adopt-and-update

# Record success only after the body ran, so a failing first-boot hook retries
# next boot instead of being permanently skipped (common#1196 new contract).
version-script-commit bootupctl-adopt privileged 1
