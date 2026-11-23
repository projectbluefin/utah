#!/usr/bin/env bash
# Build a single-architecture UEFI live ISO from a Utah bootc image.
# Usage: build-iso.sh IMAGE OUTPUT_ISO [TITLE] [DEBUG] [PUBLISHED_IMAGE]
set -euo pipefail

IMAGE="${1:?image ref is required}"
OUTPUT_ISO="${2:?output ISO path is required}"
TITLE="${3:-Utah Live}"
DEBUG="${4:-0}"
# SOURCE_IMAGE may be localhost for development, but the embedded payload and
# installer recipe use this stable, publishable reference.
PUBLISHED_IMAGE="${5:-ghcr.io/projectbluefin/utah:testing}"
# Live-ISO size budget (#684). The ISO embeds the full payload image for
# offline install as unpacked containers-storage files, which mksquashfs
# stores once because they are byte-identical to the live root; an OCI layout
# would be a second, undeduplicable copy (PR #1131 run 43870670314: 9.4-11.3
# GiB squashfs per flavor at zstd 15). Post-fix E2E run
# 35469913325 (2026-09-19) measured 3.9G (utah), 4.6G (gaming), 5.2G (nvidia),
# and 5.3G (nvidia-gaming). Diagnostic run 38175242606 (2026-10-11), at the
# default zstd level 3, measured 4.7G, 5.5G, 6.0G, and 6.1G respectively; the
# nvidia-gaming overage is tracked in #684. Run 40107791411 (2026-10-24) measured
# the main tacklebox ISO at 7,288,455,168 bytes (6.79 GiB) after the GNOME apps
# and parity packages landed, so the guard is 8 GiB while size reduction
# strategies are evaluated (#875). Keep the guard as a regression detector; the
# review-VM workflow uses a test-only compression override.
# Override per-run with UTAH_ISO_MAX_GB (GB) without editing this script.
ISO_MAX_GB="${UTAH_ISO_MAX_GB:-8}"
# Production/local callers keep the measured default; review VM can override
# this to reach the all-flavor install/boot checks while #684 tracks size.
ISO_COMPRESSION_LEVEL="${UTAH_ISO_COMPRESSION_LEVEL:-3}"
LABEL="UTAH_LIVE"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
mkdir -p "$(dirname "${OUTPUT_ISO}")"
OUTPUT_ISO="$(realpath "${OUTPUT_ISO}")"
LIVE_IMAGE="localhost/utah-live:testing"
# Not TMPDIR, and not /tmp. Assembly stages an uncompressed squashfs root of
# well over 13G, and /tmp is a tmpfs sized to a fraction of RAM -- the build
# gets most of the way through and then dies in a heap of "No space left on
# device" from cp, which reads as a broken image rather than a full staging
# area. Stage on real disk; UTAH_ISO_WORKDIR to put it somewhere else.
WORK="$(mktemp -d "${UTAH_ISO_WORKDIR:-/var/tmp}/utah-iso.XXXXXX")"
# The assembly step below runs under `podman unshare` and writes a squashfs
# root whose files belong to subordinate uids. Outside that namespace they are
# unremovable, so a plain rm here fails with Permission denied on every one of
# them -- leaving a ~13G tree behind and, because the trap is the last thing to
# run, failing the whole recipe after the ISO was written successfully.
cleanup_work() { podman unshare rm -rf "${WORK}" 2>/dev/null || rm -rf "${WORK}" 2>/dev/null || true; rm -f "${ROOT}/iso/live/src/utah-verify-desktop-contract.py" "${ROOT}/iso/live/src/images/utahraptor.png"; rmdir "${ROOT}/iso/live/src/images" 2>/dev/null || true; }
trap cleanup_work EXIT

cd "${ROOT}"
# The ISO build context is iso/live/, which cannot reach repo-root scripts/.
# Stage the Flatpak parser into the context; the Containerfile ships it in the
# overlay (persistent, so the live guest has it too), and the trap removes it.
cp scripts/verify-desktop-contract.py iso/live/src/utah-verify-desktop-contract.py
# Likewise the installer's mascot art, which is pinned in config/artwork.lock
# rather than committed. The fetch verifies its sha256 before writing it.
scripts/fetch-artwork.sh \
    assets/raster/dinos/jacob-schnurr/utah/utah-look/black-outline/utah-look-black-outline.png \
    iso/live/src/images/utahraptor.png
echo "Building live environment from ${IMAGE}"
# flatpak installs through bwrap, which needs to create a user namespace inside
# the build container; rootless podman refuses that without sys_admin, and the
# failure surfaces as an unrelated-looking "No remote refs found for flathub".
# projectbluefin/iso passes the same two flags to build the same layer.
podman build --layers \
    --cap-add sys_admin --security-opt label=disable \
    --build-arg SOURCE_IMAGE="${IMAGE}" \
    --build-arg TARGET_IMAGE="${PUBLISHED_IMAGE}" \
    --build-arg DEBUG="${DEBUG}" \
    --tag "${LIVE_IMAGE}" \
    --file iso/live/Containerfile iso/live

# Image mounts live in rootless Podman's user namespace. Keep the complete
# mount/copy/assembly operation inside podman unshare rather than leaking a
# namespace-private mount path back to the host shell.
podman unshare bash -s -- "${LIVE_IMAGE}" "${IMAGE}" "${PUBLISHED_IMAGE}" "${OUTPUT_ISO}" "${TITLE}" "${LABEL}" "${WORK}" "${ISO_MAX_GB}" "${ISO_COMPRESSION_LEVEL}" <<'ASSEMBLY'
set -euo pipefail
LIVE_IMAGE="$1"
PAYLOAD_IMAGE="$2"
PUBLISHED_IMAGE="$3"
OUTPUT_ISO="$4"
TITLE="$5"
LABEL="$6"
WORK="$7"
ISO_MAX_GB="$8"
ISO_COMPRESSION_LEVEL="$9"
MOUNT="$(podman image mount "${LIVE_IMAGE}")"
cleanup() {
    set +e
    podman image unmount "${LIVE_IMAGE}" >/dev/null 2>&1
}
trap cleanup EXIT

KERNEL="$(python3 iso/scripts/live-kernel.py "${MOUNT}" --select-bootc)"
[[ -n "${KERNEL}" ]] || { echo 'No kernel found in live image' >&2; exit 1; }
VMLINUZ="$(python3 iso/scripts/live-kernel.py "${MOUNT}" "${KERNEL}")"
INITRD="${MOUNT}/usr/lib/modules/${KERNEL}/initramfs.img"
SYSTEMD_BOOT="${MOUNT}/usr/lib/systemd/boot/efi/systemd-bootx64.efi"
for file in "${VMLINUZ}" "${INITRD}" "${SYSTEMD_BOOT}"; do
    [[ -f "${file}" ]] || { echo "Missing live boot file: ${file}" >&2; exit 1; }
done

# Start with the live image filesystem, then add the target OCI image as an
# unpacked containers-storage graphroot under usr/lib/containers/storage, plus
# the receipt (/etc/bootc-installer/media-input.json) that binds fisherman to
# it. The live system bind-mounts that store read-only at
# /var/lib/containers/storage, the one path the receipt may name; see
# embed-store-payload.sh and iso/live/src/configure-live.sh.
SQUASHFS_ROOT="${WORK}/squashfs-root"
mkdir -p "${SQUASHFS_ROOT}"
cp -a "${MOUNT}/." "${SQUASHFS_ROOT}/"
bash iso/scripts/embed-store-payload.sh "${PAYLOAD_IMAGE}" "${PUBLISHED_IMAGE}" "${SQUASHFS_ROOT}" "${WORK}" "${LIVE_IMAGE}"

SQUASHFS="${WORK}/squashfs.img"
echo "Creating live rootfs (${KERNEL})"
mksquashfs "${SQUASHFS_ROOT}" "${SQUASHFS}" \
    -noappend -comp zstd -Xcompression-level "${ISO_COMPRESSION_LEVEL}" -b 131072 -processors 4 \
    -wildcards -e 'proc/*' -e 'sys/*' -e 'dev/*' -e run -e tmp

ESP_MB=$(( $(du -m "${INITRD}" | cut -f1) + $(du -m "${VMLINUZ}" | cut -f1) + 32 ))
ESP="${WORK}/efi.img"
truncate -s "${ESP_MB}M" "${ESP}"
mkfs.fat -F 32 -n ESP "${ESP}" >/dev/null
export MTOOLS_SKIP_CHECK=1
mmd -i "${ESP}" ::/EFI ::/EFI/BOOT ::/loader ::/loader/entries ::/images ::/images/pxeboot
mcopy -i "${ESP}" "${SYSTEMD_BOOT}" ::/EFI/BOOT/BOOTX64.EFI
mcopy -i "${ESP}" "${VMLINUZ}" ::/images/pxeboot/vmlinuz
mcopy -i "${ESP}" "${INITRD}" ::/images/pxeboot/initrd.img
# Documented exception (Issue #22): rootless podman unshare cannot write security.selinux
# xattrs into the squashfs root, leaving it unlabeled. enforcing=0 is required for live boot
# to avoid systemd/GDM denials until xattr-preserving rootfs assembly is implemented.
#
# This applies for every DEBUG value, which was unremarkable while every ISO
# was disposable. Since post-testing-e2e.yml began retaining the DEBUG=0 build
# for 3 days as "production-iso-*", it is no longer only a test concern: a
# permissive live boot is a product decision, tracked with signing and Secure
# Boot in #186, and must be settled there before any of these ISOs reach
# users. DEBUG=0 does still exclude the test credentials and sshd path
# (iso/live/src/configure-live.sh), so the retained artifact carries no
# secrets; it is simply not release media.
#
# The E2E harness also depends on this: luks-e2e.sh logs in to the DEBUG=0
# live session over AF_VSOCK with an SMBIOS credential key, and an enforcing
# Utah system denies sshd that key (#1448). Making the live boot enforcing
# needs an SELinux allowance for sshd reading the credential first, or every
# luks/lifecycle/candidate/review_vm job fails at the live SSH wait.
cat > "${WORK}/utah-live.conf" <<EOF
 title   ${TITLE}
 linux   /images/pxeboot/vmlinuz
 initrd  /images/pxeboot/initrd.img
 options root=live:LABEL=${LABEL} rd.live.image rd.live.overlay.overlayfs=1 enforcing=0 console=ttyS0,115200n8
EOF
sed -i 's/^ //' "${WORK}/utah-live.conf"
printf 'timeout 5\ndefault utah-live.conf\n' > "${WORK}/loader.conf"
mcopy -i "${ESP}" "${WORK}/utah-live.conf" ::/loader/entries/utah-live.conf
mcopy -i "${ESP}" "${WORK}/loader.conf" ::/loader/loader.conf

# Assemble the ISO filesystem. Utah live media supports x86_64 UEFI boot via
# systemd-boot. BIOS/legacy MBR boot and file-backed/Ventoy loopback booting are
# unsupported; loopback configs and file-backed ISO boot parameters are deliberately omitted.
ISO_ROOT="${WORK}/iso-root"
mkdir -p "${ISO_ROOT}/EFI/BOOT" "${ISO_ROOT}/LiveOS" "${ISO_ROOT}/images/pxeboot"
cp "${SYSTEMD_BOOT}" "${ISO_ROOT}/EFI/BOOT/BOOTX64.EFI"
cp "${VMLINUZ}" "${ISO_ROOT}/images/pxeboot/vmlinuz"
cp "${INITRD}" "${ISO_ROOT}/images/pxeboot/initrd.img"
cp "${ESP}" "${ISO_ROOT}/EFI/efi.img"
cp "${SQUASHFS}" "${ISO_ROOT}/LiveOS/squashfs.img"

xorriso -as mkisofs -iso-level 3 -r -J --joliet-long -V "${LABEL}" \
    --efi-boot EFI/efi.img -efi-boot-part --efi-boot-image \
    -o "${OUTPUT_ISO}" "${ISO_ROOT}"
echo "ISO ready: ${OUTPUT_ISO} ($(du -sh "${OUTPUT_ISO}" | cut -f1))"
# Budget guard: a silent drift of nearly a gigabyte in twelve days (#128) is
# exactly what this fails closed against. Compare byte counts so the GB ceiling
# is exact regardless of how `du -h` rounds.
iso_max_bytes=$(( ISO_MAX_GB * 1024 * 1024 * 1024 ))
iso_size_bytes=$(du -b "${OUTPUT_ISO}" | cut -f1)
if [ "${iso_size_bytes}" -gt "${iso_max_bytes}" ]; then
    echo "ERROR: live ISO ($(du -sh "${OUTPUT_ISO}" | cut -f1)) exceeds ${ISO_MAX_GB} GB budget (#128)" >&2
    exit 1
fi
ASSEMBLY
