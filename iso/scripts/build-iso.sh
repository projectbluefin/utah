#!/usr/bin/env bash
# Build a single-architecture UEFI live ISO from a Utah bootc image.
# Usage: build-iso.sh IMAGE OUTPUT_ISO [TITLE] [DEBUG] [PUBLISHED_IMAGE]
set -euo pipefail

IMAGE="${1:?image ref is required}"
OUTPUT_ISO="${2:?output ISO path is required}"
TITLE="${3:-Utah Live}"
DEBUG="${4:-0}"
# SOURCE_IMAGE may be localhost for development, but the embedded store and
# installer recipe use this stable, publishable reference.
PUBLISHED_IMAGE="${5:-ghcr.io/projectbluefin/utah:testing}"
# Live-ISO size budget (#128). The ISO embeds the full container store for
# offline install, so image growth shows up doubled on the ISO. The last fully
# passing run (2026-09-06) was 7.7G; the 2026-09-18 run was 8.6G once the
# ~4 GB package repository stopped being removed from the image. The ceiling
# sits between those two: 8 GB is above the last passing run so a clean build
# passes, but below the grown size, so the exact #128 regression fails the job
# instead of landing silently. Raise N only after the real fix -- unmount,
# never COPY, the package repository (#128) -- lands; #105 adds linux-firmware
# and ~30 parity packages on top, so expect to revisit N.
# Override per-run with UTAH_ISO_MAX_GB (GB) without editing this script.
ISO_MAX_GB="${UTAH_ISO_MAX_GB:-8}"
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
cleanup_work() { podman unshare rm -rf "${WORK}" 2>/dev/null || rm -rf "${WORK}" 2>/dev/null || true; }
trap cleanup_work EXIT

cd "${ROOT}"
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
podman unshare bash -s -- "${LIVE_IMAGE}" "${IMAGE}" "${PUBLISHED_IMAGE}" "${OUTPUT_ISO}" "${TITLE}" "${LABEL}" "${WORK}" "${ISO_MAX_GB}" <<'ASSEMBLY'
set -euo pipefail
LIVE_IMAGE="$1"
PAYLOAD_IMAGE="$2"
PUBLISHED_IMAGE="$3"
OUTPUT_ISO="$4"
TITLE="$5"
LABEL="$6"
WORK="$7"
ISO_MAX_GB="$8"
MOUNT="$(podman image mount "${LIVE_IMAGE}")"
cleanup() {
    set +e
    podman image unmount "${LIVE_IMAGE}" >/dev/null 2>&1
}
trap cleanup EXIT

KERNEL="$(find "${MOUNT}/usr/lib/modules" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' | sort -V | tail -1)"
[[ -n "${KERNEL}" ]] || { echo 'No kernel found in live image' >&2; exit 1; }
VMLINUZ="$(python3 iso/scripts/live-kernel.py "${MOUNT}" "${KERNEL}")"
INITRD="${MOUNT}/usr/lib/modules/${KERNEL}/initramfs.img"
SYSTEMD_BOOT="${MOUNT}/usr/lib/systemd/boot/efi/systemd-bootx64.efi"
for file in "${VMLINUZ}" "${INITRD}" "${SYSTEMD_BOOT}"; do
    [[ -f "${file}" ]] || { echo "Missing live boot file: ${file}" >&2; exit 1; }
done

# Start with the live image filesystem, then add the target OCI image as a VFS
# containers-storage graphroot. This is Dakota's offline-payload design adapted
# for Utah's conventional bootc (non-composefs) install path.
SQUASHFS_ROOT="${WORK}/squashfs-root"
mkdir -p "${SQUASHFS_ROOT}"
cp -a "${MOUNT}/." "${SQUASHFS_ROOT}/"

PAYLOAD_EXPORT="${WORK}/utah-payload"
PAYLOAD_STORE="${WORK}/payload-store"
STORAGE_CONF="${WORK}/payload-storage.conf"
mkdir -p "${PAYLOAD_STORE}"
# overlay, and /usr/lib/containers/storage, because that is what the image
# already resolves to. Hummingbird ships a vendor drop-in
# (/usr/share/containers/storage.conf.d/00-vendor.conf) that sets
# driver = "overlay", and drop-ins are applied after /etc/containers/storage.conf
# -- so a storage.conf written into the live layer cannot move the driver, and
# podman looks for images in the vendor imagestore no matter what the live
# environment asks for. Writing the payload anywhere else means the installer
# does not find it and falls back to pulling from a registry.
printf '[storage]\ndriver = "overlay"\nrunroot = "/tmp/cs-runroot"\ngraphroot = "/payload-store"\n' >"${STORAGE_CONF}"
echo "Embedding ${PUBLISHED_IMAGE} for offline installation"
# Containers-storage exports uncompressed layers; recompression or OCI archive
# conversion changes the manifest digest. For immutable CI inputs, export the
# original registry blobs directly and retain their manifest bytes via dir.
payload_source="containers-storage:${PAYLOAD_IMAGE}"
copy_flags=(--remove-signatures)
if [[ "${PUBLISHED_IMAGE}" == *@sha256:* ]]; then
    [[ "${PAYLOAD_IMAGE}" == "${PUBLISHED_IMAGE}" ]] || {
        echo 'Digest-pinned live image and offline payload must match' >&2; exit 1;
    }
    payload_source="docker://${PUBLISHED_IMAGE}"
    copy_flags+=(--preserve-digests)
fi
skopeo copy "${copy_flags[@]}" "${payload_source}" \
    "dir:${PAYLOAD_EXPORT}"
podman run --rm --privileged \
    -v "${PAYLOAD_EXPORT}:/payload:ro" \
    -v "${PAYLOAD_STORE}:/payload-store" \
    -v "${STORAGE_CONF}:/tmp/storage.conf:ro" \
    "${LIVE_IMAGE}" sh -c 'mkdir -p /tmp/cs-runroot /var/tmp && CONTAINERS_STORAGE_CONF=/tmp/storage.conf skopeo copy --preserve-digests dir:/payload "containers-storage:$1"' sh "${PUBLISHED_IMAGE}"
mkdir -p "${SQUASHFS_ROOT}/usr/lib/containers/storage"
cp -a "${PAYLOAD_STORE}/." "${SQUASHFS_ROOT}/usr/lib/containers/storage/"
rm -rf "${PAYLOAD_EXPORT}" "${PAYLOAD_STORE}" "${STORAGE_CONF}"

SQUASHFS="${WORK}/squashfs.img"
echo "Creating live rootfs (${KERNEL})"
mksquashfs "${SQUASHFS_ROOT}" "${SQUASHFS}" \
    -noappend -comp zstd -Xcompression-level 3 -b 131072 -processors 4 \
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
