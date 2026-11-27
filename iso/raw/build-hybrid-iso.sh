#!/usr/bin/env bash
# Build the hybrid ISO of Utah's installer medium (#1404, Secure Boot off): the
# raw medium (build-raw-image.sh, then bake-flatpaks.sh and
# shrink-raw-image.sh) made bootable from optical media too, still a GPT disk
# image that boots from USB and that the installer block-copies.
#
# Usage (root, from the repository root): build-hybrid-iso.sh RAW_IMAGE OUT_ISO
#
# Host tools: util-linux (losetup, mount, unshare, blkid, blockdev, sfdisk,
# findmnt), coreutils (chroot), diffutils (diff, cmp), python3, udev
# (udevadm); the kernel's loop, vfat, ext4 and btrfs drivers, plus erofs and
# overlay for a composefs medium. No container: systemd-repart and
# systemd-boot come from the medium's own deployment, mounted read-only.
#
# The ISO is the raw medium byte for byte except for two things:
#
#   1. The ESP also boots systemd-boot. GRUB boots the raw medium from the
#      separate ext4 /boot, and firmware booting El Torito media exposes only
#      the El Torito image (the ESP), so the kernel has to be on the ESP:
#      EFI/BOOT/BOOTX64.EFI becomes the image's unsigned systemd-boot
#      (systemd-boot-unsigned; Secure Boot off until a signed chain lands,
#      #1380), loader/entries/utah-live.conf is GRUB's live entry with the
#      kernel and initrd copied to /utah-live (medium.py iso-entry), and the
#      medium's shim moves to /utah-live/BOOTX64.EFI. utah-sysinstall-post
#      moves shim back and removes /utah-live and /loader from the installed
#      copy of the ESP, so the installed system boots shim -> GRUB as before.
#   2. An El Torito boot catalog for EFI, pointing at the ESP, written by the
#      medium's own systemd-repart (`--el-torito`, systemd 261) into the gap
#      between the GPT and the first partition: ISO9660 descriptors at block
#      16 (32 KiB), the medium's first partition at 1 MiB. The GPT, partition
#      UUIDs and filesystems are untouched (checked below), so the installer
#      (utah-live-definitions) copies the same partitions from the ISO.
#
# Booted from a USB disk, the kernel reads the GPT as on the raw medium. A
# CD-ROM has 2048-byte sectors and the GPT is written for 512, so the kernel
# finds no partitions there: systemd-boot exports the ESP's PARTUUID
# (LoaderDevicePartUUID, found from the El Torito device path), udev's blkid
# builtin flags the drive (ID_PART_GPT_AUTO_ROOT_DISK_NEEDS_LOOP) and
# systemd-loop@.service attaches a partitioned loop device over it in the
# initrd, where root=UUID= finds the medium's root (the live layer puts both
# in the initramfs, 60-utah-live.conf).
#
# Environment:
#   UTAH_RAW_EVIDENCE   evidence directory (default: OUT_ISO's directory/raw-evidence):
#                       iso.json ({"iso_bytes", "build_seconds"}), iso-entry.conf,
#                       iso-loader.conf, iso-esp-files.txt, iso-repart.json
set -euo pipefail

RAW="${1:?raw medium path is required}"
OUT="${2:?output ISO path is required}"
EVIDENCE="${UTAH_RAW_EVIDENCE:-$(dirname "${OUT}")/raw-evidence}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ESP_TYPE=c12a7328-f81f-11d2-ba4b-00a0c93ec93b
LIVE_ENTRY=utah-live
ESP_DIR="$(python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); import medium; print(medium.ISO_ESP_DIR)' "${HERE}")"
SDBOOT=usr/lib/systemd/boot/efi/systemd-bootx64.efi
VOLUME=UTAH_INSTALLER

[[ ${EUID} -eq 0 ]] || { echo "run as root (loop devices, mounts)" >&2; exit 1; }
start=$(date +%s)
RAW="$(realpath "${RAW}")"
OUT="$(realpath -m "${OUT}")"
mkdir -p "$(dirname "${OUT}")" "${EVIDENCE}"
size="$(stat -c %s "${RAW}")"
(( size % 2048 == 0 )) || { echo "${RAW} is ${size} bytes, not whole 2048-byte blocks" >&2; exit 1; }

partial="${OUT}.partial"
work="$(mktemp -d)"
src="" dst=""
cleanup() {
    set +e
    findmnt -rno TARGET | grep -F "${work}/" | sort -r | while read -r m; do umount "$m"; done
    [[ -n "${dst}" ]] && losetup -d "${dst}"
    [[ -n "${src}" ]] && losetup -d "${src}"
    rm -rf --one-file-system "${work}" "${partial}"
}
trap cleanup EXIT

rm -f "${OUT}" "${partial}"
cp --sparse=always "${RAW}" "${partial}"
src="$(losetup --find --show --read-only --partscan "${RAW}")"
dst="$(losetup --find --show --partscan "${partial}")"
udevadm settle

esp="" boot="" root=""
for p in "${src}"p*; do
    ptype="$(blkid -p -s PART_ENTRY_TYPE -o value "$p" | tr '[:upper:]' '[:lower:]')"
    fstype="$(blkid -p -s TYPE -o value "$p" || true)"
    if [[ "${ptype}" == "${ESP_TYPE}" ]]; then esp="$p"
    elif [[ "${fstype}" == ext4 ]]; then boot="$p"
    elif [[ "${fstype}" == btrfs ]]; then root="$p"
    fi
done
[[ -n "${esp}" && -n "${root}" ]] || { echo "unexpected layout: esp=${esp} boot=${boot} root=${root}" >&2; exit 1; }
esp_start=$(( $(cat "/sys/class/block/${esp##*/}/start") * 512 ))
esp_bytes="$(blockdev --getsize64 "${esp}")"
(( esp_start % 2048 == 0 )) || { echo "the ESP starts at byte ${esp_start}, not on a 2048-byte block" >&2; exit 1; }

# --- The medium's deployment and its live entry, read-only. -------------------
mkdir -p "${work}/root" "${work}/boot" "${work}/meta" "${work}/deploy" "${work}/esp"
mount -o ro,rescue=nologreplay "${root}" "${work}/root"
bootdir="${work}/root/boot"
if [[ -n "${boot}" ]]; then
    mount -o ro,noload "${boot}" "${work}/boot"
    bootdir="${work}/boot"
fi
if [[ -d "${work}/root/composefs/images" ]]; then
    # composefs: the deployment's erofs image over the object store, as
    # bootc's initramfs-setup mounts it (objects as overlayfs data-only layer).
    deploys=("${work}"/root/state/deploy/*)
    [[ ${#deploys[@]} -eq 1 ]] || { echo "expected one composefs deployment, found: ${deploys[*]}" >&2; exit 1; }
    mount -t erofs -o ro,loop "${work}/root/composefs/images/${deploys[0]##*/}" "${work}/meta"
    mount -t overlay overlay -o "ro,metacopy=on,redirect_dir=on,lowerdir=${work}/meta::${work}/root/composefs/objects" "${work}/deploy"
    deploy="${work}/deploy"
else
    deploys=("${work}"/root/ostree/deploy/*/deploy/*.0)
    [[ ${#deploys[@]} -eq 1 && -d "${deploys[0]}/usr" ]] || { echo "expected one ostree deployment, found: ${deploys[*]}" >&2; exit 1; }
    deploy="${deploys[0]}"
fi
[[ -f "${deploy}/${SDBOOT}" ]] || { echo "the deployment has no /${SDBOOT} (systemd-boot-unsigned)" >&2; exit 1; }
live_entry="${bootdir}/loader/entries/${LIVE_ENTRY}.conf"
[[ -f "${live_entry}" ]] || { echo "no ${LIVE_ENTRY}.conf in the medium's /boot/loader/entries" >&2; exit 1; }

# --- systemd-boot on the copy's ESP. ------------------------------------------
dst_esp="${dst}p$(cat "/sys/class/block/${esp##*/}/partition")"
mount "${dst_esp}" "${work}/esp"
efi="${work}/esp"
[[ ! -e "${efi}/${ESP_DIR}" && ! -e "${efi}/loader" ]] \
    || { echo "the medium's ESP already has /${ESP_DIR} or /loader" >&2; exit 1; }
[[ -f "${efi}/EFI/BOOT/BOOTX64.EFI" ]] || { echo "the medium's ESP has no EFI/BOOT/BOOTX64.EFI" >&2; exit 1; }
install -d "${efi}/${ESP_DIR}" "${efi}/loader/entries"
python3 "${HERE}/medium.py" iso-entry "${live_entry}" "${efi}/loader/entries/${LIVE_ENTRY}.conf" >"${work}/files"
while read -r dest from; do
    install -m 0644 "${bootdir}/${from#/}" "${efi}${dest}"
done <"${work}/files"
mv "${efi}/EFI/BOOT/BOOTX64.EFI" "${efi}/${ESP_DIR}/BOOTX64.EFI"
install -m 0644 "${deploy}/${SDBOOT}" "${efi}/EFI/BOOT/BOOTX64.EFI"
python3 "${HERE}/medium.py" iso-loader-conf "${efi}/loader/loader.conf"
cp "${efi}/loader/entries/${LIVE_ENTRY}.conf" "${EVIDENCE}/iso-entry.conf"
cp "${efi}/loader/loader.conf" "${EVIDENCE}/iso-loader.conf"
{
    find "${efi}" -type f -printf '%s\t%P\n' | sort -k2
    df -B1 --output=size,used,avail "${efi}"
} | tee "${EVIDENCE}/iso-esp-files.txt"
sync
umount "${efi}"
losetup -d "${dst}"
dst=""

# --- El Torito, by the medium's systemd-repart. -------------------------------
# One definition matching the existing ESP at its size, so repart changes no
# partition and only writes the boot catalog for it; the other partitions are
# foreign to it and left alone.
mkdir -p "${work}/repart.d"
printf '[Partition]\nType=esp\nSizeMinBytes=%s\nSizeMaxBytes=%s\n' "${esp_bytes}" "${esp_bytes}" \
    >"${work}/repart.d/10-esp.conf"
unshare --mount --propagation private -- bash -euc '
    d="$1" image="$2" defs="$3" volume="$4"
    mount -t proc proc "$d/proc"
    mount --rbind /dev "$d/dev"
    mount -t sysfs sysfs "$d/sys"
    mount -t tmpfs tmpfs "$d/run"
    mount -t tmpfs tmpfs "$d/tmp"
    mkdir "$d/run/repart.d"
    cp "$defs"/*.conf "$d/run/repart.d/"
    touch "$d/run/medium.iso"
    mount --bind "$image" "$d/run/medium.iso"
    chroot "$d" systemd-repart --version | head -1 >&2
    chroot "$d" systemd-repart --definitions=/run/repart.d --dry-run=no --offline=yes --seed=random \
        --el-torito=yes --el-torito-system=LINUX --el-torito-volume="$volume" \
        --el-torito-publisher=PROJECTBLUEFIN --no-pager --json=pretty /run/medium.iso
' bash "${deploy}" "${partial}" "${work}/repart.d" "${VOLUME}" | tee "${EVIDENCE}/iso-repart.json"

# --- Checks: El Torito boots the ESP, and nothing else moved. -----------------
rba="$(python3 "${HERE}/medium.py" eltorito-rba "${partial}")"
[[ "${rba}" == "$(( esp_start / 2048 ))" ]] \
    || { echo "El Torito loads block ${rba}, the ESP starts at block $(( esp_start / 2048 ))" >&2; exit 1; }
table() { sfdisk --dump "$1" | sed -E "s|^$1([0-9]+) |P\\1 |; /^device:/d"; }
diff <(table "${RAW}") <(table "${partial}") \
    || { echo "the partition table changed" >&2; exit 1; }
[[ "$(stat -c %s "${partial}")" == "${size}" ]] || { echo "the image size changed" >&2; exit 1; }
for p in "${src}"p*; do
    [[ "$p" == "${esp}" ]] && continue
    n="$(cat "/sys/class/block/${p##*/}/partition")"
    offset=$(( $(cat "/sys/class/block/${p##*/}/start") * 512 ))
    bytes="$(blockdev --getsize64 "$p")"
    cmp -s -n "${bytes}" "$p" <(tail -c +"$(( offset + 1 ))" "${partial}") \
        || { echo "partition ${n} differs from the raw medium's" >&2; exit 1; }
done

mv "${partial}" "${OUT}"
build_seconds=$(( $(date +%s) - start ))
printf '{"iso_bytes": %s, "build_seconds": %s}\n' "$(stat -c %s "${OUT}")" "${build_seconds}" \
    | tee "${EVIDENCE}/iso.json"
echo "Hybrid ISO ready: ${OUT} (El Torito -> ESP at block ${rba})"
