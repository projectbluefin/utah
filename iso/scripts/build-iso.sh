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
LABEL="UTAH_LIVE"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
mkdir -p "$(dirname "${OUTPUT_ISO}")"
OUTPUT_ISO="$(realpath "${OUTPUT_ISO}")"
LIVE_IMAGE="localhost/utah-live:testing"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/utah-iso.XXXXXX")"
trap 'rm -rf "${WORK}"' EXIT

cd "${ROOT}"
echo "Building live environment from ${IMAGE}"
podman build --layers \
    --build-arg SOURCE_IMAGE="${IMAGE}" \
    --build-arg TARGET_IMAGE="${PUBLISHED_IMAGE}" \
    --build-arg DEBUG="${DEBUG}" \
    --tag "${LIVE_IMAGE}" \
    --file iso/live/Containerfile iso/live

# Image mounts live in rootless Podman's user namespace. Keep the complete
# mount/copy/assembly operation inside podman unshare rather than leaking a
# namespace-private mount path back to the host shell.
podman unshare bash -s -- "${LIVE_IMAGE}" "${IMAGE}" "${PUBLISHED_IMAGE}" "${OUTPUT_ISO}" "${TITLE}" "${LABEL}" "${WORK}" <<'ASSEMBLY'
set -euo pipefail
LIVE_IMAGE="$1"
PAYLOAD_IMAGE="$2"
PUBLISHED_IMAGE="$3"
OUTPUT_ISO="$4"
TITLE="$5"
LABEL="$6"
WORK="$7"
MOUNT="$(podman image mount "${LIVE_IMAGE}")"
cleanup() {
    set +e
    podman image unmount "${LIVE_IMAGE}" >/dev/null 2>&1
}
trap cleanup EXIT

KERNEL="$(find "${MOUNT}/usr/lib/modules" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' | sort -V | tail -1)"
[[ -n "${KERNEL}" ]] || { echo 'No kernel found in live image' >&2; exit 1; }
VMLINUZ="${MOUNT}/usr/lib/modules/${KERNEL}/vmlinuz"
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

PAYLOAD_ARCHIVE="${WORK}/utah-payload.oci.tar"
PAYLOAD_STORE="${WORK}/vfs-store"
STORAGE_CONF="${WORK}/vfs-storage.conf"
mkdir -p "${PAYLOAD_STORE}"
printf '[storage]\ndriver = "vfs"\nrunroot = "/tmp/cs-runroot"\ngraphroot = "/vfs-store"\n' >"${STORAGE_CONF}"
echo "Embedding ${PUBLISHED_IMAGE} for offline installation"
skopeo copy --remove-signatures \
    "containers-storage:${PAYLOAD_IMAGE}" \
    "oci-archive:${PAYLOAD_ARCHIVE}:${PUBLISHED_IMAGE}"
podman run --rm --privileged \
    -v "${PAYLOAD_ARCHIVE}:/payload.oci.tar:ro" \
    -v "${PAYLOAD_STORE}:/vfs-store" \
    -v "${STORAGE_CONF}:/tmp/storage.conf:ro" \
    "${LIVE_IMAGE}" sh -c "mkdir -p /tmp/cs-runroot /var/tmp && CONTAINERS_STORAGE_CONF=/tmp/storage.conf skopeo copy oci-archive:/payload.oci.tar:${PUBLISHED_IMAGE} containers-storage:${PUBLISHED_IMAGE}"
mkdir -p "${SQUASHFS_ROOT}/var/lib/containers/storage"
cp -a "${PAYLOAD_STORE}/." "${SQUASHFS_ROOT}/var/lib/containers/storage/"
rm -rf "${PAYLOAD_ARCHIVE}" "${PAYLOAD_STORE}" "${STORAGE_CONF}"

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

ISO_ROOT="${WORK}/iso-root"
mkdir -p "${ISO_ROOT}/EFI/BOOT" "${ISO_ROOT}/LiveOS" "${ISO_ROOT}/images/pxeboot" "${ISO_ROOT}/boot/grub"
cp "${SYSTEMD_BOOT}" "${ISO_ROOT}/EFI/BOOT/BOOTX64.EFI"
cp "${VMLINUZ}" "${ISO_ROOT}/images/pxeboot/vmlinuz"
cp "${INITRD}" "${ISO_ROOT}/images/pxeboot/initrd.img"
cp "${ESP}" "${ISO_ROOT}/EFI/efi.img"
cp "${SQUASHFS}" "${ISO_ROOT}/LiveOS/squashfs.img"
cat > "${ISO_ROOT}/boot/grub/loopback.cfg" <<EOF
menuentry "${TITLE}" {
    linux /images/pxeboot/vmlinuz root=live:LABEL=${LABEL} rd.live.image rd.live.overlay.overlayfs=1 enforcing=0 console=ttyS0,115200n8 rd.utah.isofile=\${iso_path}
    initrd /images/pxeboot/initrd.img
}
EOF

xorriso -as mkisofs -iso-level 3 -r -J --joliet-long -V "${LABEL}" \
    --efi-boot EFI/efi.img -efi-boot-part --efi-boot-image \
    -o "${OUTPUT_ISO}" "${ISO_ROOT}"
echo "ISO ready: ${OUTPUT_ISO} ($(du -sh "${OUTPUT_ISO}" | cut -f1))"
ASSEMBLY
