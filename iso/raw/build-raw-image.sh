#!/usr/bin/env bash
# Build the Utah raw disk image that is both the live medium and the install
# payload of the experimental image-based installer (docs/adr/0001).
#
# Usage (root): build-raw-image.sh IMAGE OUTPUT_RAW [TARGET_IMGREF]
#
#   1. `bootc install to-filesystem` the image into OUTPUT_RAW, partitioned
#      the way an encrypted install needs it: ESP, a separate ext4 /boot and a
#      btrfs root. The installer encrypts only the root, so GRUB keeps reading
#      an unencrypted /boot (it cannot unlock systemd-repart's LUKS2/argon2id;
#      `bootc install to-disk --filesystem btrfs` puts /boot inside the root).
#      This is what fisherman does at install time today, done once at build
#      time.
#   2. Add a "Utah Live" BLS entry: the deployment's own entry with `ro
#      utah.live` instead of `rw` (utah-live-root.service overlays the root),
#      made the GRUB default through grubenv (medium.py). OS-Installer's Utah
#      script removes both from the installed copy.
#   3. Install the removable-media EFI fallback (shim as BOOTX64.EFI) so the
#      medium boots on firmware with no NVRAM entry for it.
#
# Environment:
#   UTAH_RAW_SIZE            sparse image size (default 24G)
#   UTAH_RAW_LIVE_KARGS      extra kargs for the live entry only (CI: utah.live.debug)
#   UTAH_RAW_SSH_PUBKEY      CI only: root authorized key (bootc --root-ssh-authorized-keys)
#   UTAH_RAW_DEBUG_SSHD=1    CI only: enable sshd as a local /etc change of the
#                            deployment, so it survives `bootc switch`
#   UTAH_RAW_EVIDENCE        directory for partition table, entries and sizes
#
# The root is written uncompressed: shrink-raw-image.sh compresses it after
# the Flatpaks are baked (UTAH_RAW_BTRFS_COMPRESS), so they are covered too.
set -euo pipefail

IMAGE="${1:?image ref is required}"
OUT="${2:?output raw path is required}"
TARGET_IMGREF="${3:-ghcr.io/projectbluefin/utah:testing}"
SIZE="${UTAH_RAW_SIZE:-24G}"
LIVE_KARGS="${UTAH_RAW_LIVE_KARGS:-}"
EVIDENCE="${UTAH_RAW_EVIDENCE:-$(dirname "${OUT}")/raw-evidence}"
ESP_TYPE=c12a7328-f81f-11d2-ba4b-00a0c93ec93b
LIVE_ENTRY=utah-live
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

[[ ${EUID} -eq 0 ]] || { echo "run as root (bootc install, loop devices)" >&2; exit 1; }
mkdir -p "$(dirname "${OUT}")" "${EVIDENCE}"
OUT="$(realpath -m "${OUT}")"
data="$(dirname "${OUT}")"

rm -f "${OUT}"
truncate -s "${SIZE}" "${OUT}"
# The medium's partition table and the deployment's install-time kargs are
# the contract in medium.py (LAYOUT, INSTALL_KARGS), held by
# tests/test_image_installer.py.
python3 "${HERE}/medium.py" layout | sfdisk --quiet "${OUT}"
loop="$(losetup --find --show --partscan "${OUT}")"
mnt="$(mktemp -d)"
cleanup() {
    set +e
    for m in "${mnt}"/*; do mountpoint -q "$m" && umount "$m"; done
    losetup -d "${loop}"
    rm -rf "${mnt}"
}
trap cleanup EXIT
udevadm settle

install_args=(--generic-image --target-imgref "${TARGET_IMGREF}")
mapfile -t kargs < <(python3 "${HERE}/medium.py" install-kargs)
[[ ${#kargs[@]} -gt 0 ]] || { echo "medium.py install-kargs printed nothing" >&2; exit 1; }
for karg in "${kargs[@]}"; do install_args+=(--karg "${karg}"); done
volumes=(-v /dev:/dev -v /var/lib/containers:/var/lib/containers -v "${data}:/data")
if [[ -n "${UTAH_RAW_SSH_PUBKEY:-}" ]]; then
    install -m 0644 "${UTAH_RAW_SSH_PUBKEY}" "${data}/root-key.pub"
    install_args+=(--root-ssh-authorized-keys /data/root-key.pub)
fi
echo "bootc install to-filesystem ${IMAGE} -> ${OUT} (${SIZE}, target ${TARGET_IMGREF})"
start=$(date +%s)
# mkfs and mounts run in the image itself: its btrfs-progs/e2fsprogs/dosfstools
# match what the installed system will run, and bootc sees the mounts directly.
podman run --rm --privileged --pid=host "${volumes[@]}" \
    --security-opt label=type:unconfined_t -e LOOP="${loop}" \
    "${IMAGE}" bash -euxc '
        mkfs.fat -F 32 -n EFI-SYSTEM "${LOOP}p2" >/dev/null
        mkfs.ext4 -q -L boot "${LOOP}p3"
        mkfs.btrfs -q -L root "${LOOP}p4"
        mkdir -p /target
        mount "${LOOP}p4" /target
        mkdir /target/boot
        mount "${LOOP}p3" /target/boot
        mkdir /target/boot/efi
        mount "${LOOP}p2" /target/boot/efi
        bootc install to-filesystem "$@" /target
        umount -R /target
    ' bash "${install_args[@]}"
install_seconds=$(( $(date +%s) - start ))
echo "bootc install to-filesystem took ${install_seconds}s" | tee "${EVIDENCE}/install-time.txt"
rm -f "${data}/root-key.pub"

sfdisk --dump "${loop}" | tee "${EVIDENCE}/partitions.txt"
blkid "${loop}"p* | tee "${EVIDENCE}/blkid.txt"

esp="" boot="" root=""
for p in "${loop}"p*; do
    ptype="$(blkid -p -s PART_ENTRY_TYPE -o value "$p" | tr '[:upper:]' '[:lower:]')"
    fstype="$(blkid -p -s TYPE -o value "$p" || true)"
    if [[ "${ptype}" == "${ESP_TYPE}" ]]; then esp="$p"
    elif [[ "${fstype}" == ext4 ]]; then boot="$p"
    elif [[ "${fstype}" == btrfs ]]; then root="$p"
    fi
done
[[ -n "${esp}" && -n "${root}" ]] || { echo "unexpected layout: esp=${esp} boot=${boot} root=${root}" >&2; exit 1; }
mkdir -p "${mnt}/boot" "${mnt}/efi" "${mnt}/root"
mount "${root}" "${mnt}/root"
mount "${esp}" "${mnt}/efi"
# Kept layout-agnostic: older media put /boot inside the btrfs root.
bootdir="${mnt}/root/boot"
if [[ -n "${boot}" ]]; then
    mount "${boot}" "${mnt}/boot"
    bootdir="${mnt}/boot"
fi

# --- Live entry ---------------------------------------------------------------
mapfile -t entries < <(find "${bootdir}/loader/entries/" -name '*.conf' ! -name "${LIVE_ENTRY}.conf")
[[ ${#entries[@]} -eq 1 ]] || { echo "expected one deployment entry, found ${#entries[@]}" >&2; exit 1; }
python3 "${HERE}/medium.py" live-entry "${entries[0]}" "${bootdir}/loader/entries/${LIVE_ENTRY}.conf" "${LIVE_KARGS}"
cat "${entries[0]}" >"${EVIDENCE}/entry-installed.conf"
cat "${bootdir}/loader/entries/${LIVE_ENTRY}.conf" | tee "${EVIDENCE}/entry-live.conf"

grubenv="${bootdir}/grub2/grubenv"
python3 "${HERE}/medium.py" grubenv "${grubenv}" "${LIVE_ENTRY}"
grep -av '^#' "${grubenv}" | tee "${EVIDENCE}/grubenv.txt" || true

# --- Removable-media fallback -------------------------------------------------
vendor="$(find "${mnt}/efi/EFI" -mindepth 2 -maxdepth 2 -name shimx64.efi -printf '%h\n' | head -1)"
[[ -n "${vendor}" ]] || { echo "no shimx64.efi on the ESP" >&2; exit 1; }
install -d "${mnt}/efi/EFI/BOOT"
[[ -f "${mnt}/efi/EFI/BOOT/BOOTX64.EFI" ]] || install -m 0644 "${vendor}/shimx64.efi" "${mnt}/efi/EFI/BOOT/BOOTX64.EFI"
[[ -f "${mnt}/efi/EFI/BOOT/grubx64.efi" ]] || install -m 0644 "${vendor}/grubx64.efi" "${mnt}/efi/EFI/BOOT/grubx64.efi"
find "${mnt}/efi" -type f | sed "s|${mnt}/efi||" | sort | tee "${EVIDENCE}/esp-files.txt"
for f in "${mnt}"/efi/EFI/*/grub.cfg "${mnt}"/efi/EFI/*/bootuuid.cfg "${bootdir}"/grub2/*.cfg; do
    [[ -f "$f" ]] && { echo "== ${f#"${mnt}"/}"; cat "$f"; }
done >"${EVIDENCE}/grub-configs.txt"

# --- CI-only diagnostics on the deployment ------------------------------------
for d in "${mnt}"/root/ostree/deploy/*/deploy/*.0; do
    cat "$d/etc/fstab" >"${EVIDENCE}/fstab.txt" 2>/dev/null || true
    printf '%s\n' "machine-id: '$(cat "$d/etc/machine-id" 2>/dev/null)'" >>"${EVIDENCE}/fstab.txt"
    if [[ "${UTAH_RAW_DEBUG_SSHD:-0}" == 1 ]]; then
        install -d "$d/etc/systemd/system/graphical.target.wants"
        ln -sfn /usr/lib/systemd/system/sshd.service "$d/etc/systemd/system/graphical.target.wants/sshd.service"
    fi
done
find "${mnt}"/root/ostree/deploy/*/deploy -maxdepth 1 -name '*.origin' -exec cat {} + >"${EVIDENCE}/origin.txt"
sync
umount "${mnt}/efi"
[[ -n "${boot}" ]] && umount "${mnt}/boot"
umount "${mnt}/root"

{
    echo "apparent: $(du -h --apparent-size "${OUT}" | cut -f1)"
    echo "allocated: $(du -h "${OUT}" | cut -f1)"
} | tee "${EVIDENCE}/size.txt"
# Read by image-installer.yml's comparison (iso/raw/compare_paths.py).
printf '{"raw_apparent_bytes": %s, "raw_allocated_bytes": %s, "bootc_install_seconds": %s}\n' \
    "$(du -b --apparent-size "${OUT}" | cut -f1)" "$(( $(du -k "${OUT}" | cut -f1) * 1024 ))" \
    "${install_seconds}" | tee "${EVIDENCE}/medium.json"
echo "Raw image ready: ${OUT}"
