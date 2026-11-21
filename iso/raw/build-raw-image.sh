#!/usr/bin/env bash
# Build the Utah raw disk image that is both the live medium and the install
# payload (GNOME OS-style install spike).
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
#      utah.live` instead of `rw` (utah-live-root.service overlays the root), made the GRUB
#      default through grubenv. OS-Installer's Utah script removes both from
#      the installed copy.
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
set -euo pipefail

IMAGE="${1:?image ref is required}"
OUT="${2:?output raw path is required}"
TARGET_IMGREF="${3:-ghcr.io/projectbluefin/utah:testing}"
SIZE="${UTAH_RAW_SIZE:-24G}"
LIVE_KARGS="${UTAH_RAW_LIVE_KARGS:-}"
EVIDENCE="${UTAH_RAW_EVIDENCE:-$(dirname "${OUT}")/raw-evidence}"
ESP_TYPE=c12a7328-f81f-11d2-ba4b-00a0c93ec93b
LIVE_ENTRY=utah-live

[[ ${EUID} -eq 0 ]] || { echo "run as root (bootc install, loop devices)" >&2; exit 1; }
mkdir -p "$(dirname "${OUT}")" "${EVIDENCE}"
OUT="$(realpath -m "${OUT}")"
data="$(dirname "${OUT}")"

rm -f "${OUT}"
truncate -s "${SIZE}" "${OUT}"
# BIOS-BOOT only because bootupd installs its BIOS component when it finds one;
# the installer drops it (UEFI-only target).
sfdisk --quiet "${OUT}" <<'EOF'
label: gpt
size=1MiB, type=21686148-6449-6E6F-744E-656564454649, name="BIOS-BOOT"
size=512MiB, type=C12A7328-F81F-11D2-BA4B-00A0C93EC93B, name="EFI-SYSTEM"
size=1GiB, type=0FC63DAF-8483-4772-8E79-3D69D8477DE4, name="boot"
type=4F68BCE3-E8CD-4DB1-96E7-FBCAF984B709, name="root"
EOF
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

# rhgb quiet: the graphical, branded Plymouth splash (and LUKS prompt). The
# image's kargs.d does not set them; fisherman adds them with bootSplash
# (docs/skills/desktop-contract/references/boot-splash.md). Install-time kargs
# carry over bootc upgrades.
install_args=(--generic-image --target-imgref "${TARGET_IMGREF}" --karg rhgb --karg quiet)
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
echo "bootc install to-filesystem took $(( $(date +%s) - start ))s" | tee "${EVIDENCE}/install-time.txt"
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
python3 - "${entries[0]}" "${bootdir}/loader/entries/${LIVE_ENTRY}.conf" "${LIVE_KARGS}" <<'PY'
import sys
src, dst, extra = sys.argv[1], sys.argv[2], sys.argv[3].split()
out = []
for line in open(src).read().splitlines():
    key, _, value = line.partition(" ")
    if key == "title":
        line = "title Utah Live (try or install)"
    elif key == "version":
        # GRUB's blscfg sorts by version, highest first: list the live entry first.
        line = f"version {int(value.strip()) + 1000}" if value.strip().isdigit() else line
    elif key == "options":
        args = [a for a in value.split() if a not in ("rw", "ro")]
        line = "options " + " ".join(args + ["ro", "utah.live"] + extra)
    out.append(line)
open(dst, "w").write("\n".join(out) + "\n")
PY
cat "${entries[0]}" >"${EVIDENCE}/entry-installed.conf"
cat "${bootdir}/loader/entries/${LIVE_ENTRY}.conf" | tee "${EVIDENCE}/entry-live.conf"

grubenv="${bootdir}/grub2/grubenv"
python3 - "${grubenv}" "${LIVE_ENTRY}" <<'PY'
import os, sys
path, live = sys.argv[1], sys.argv[2]
lines = []
if os.path.exists(path):
    lines = [l for l in open(path).read().split("\n") if l and not l.startswith("#") and not l.startswith("saved_entry=")]
body = "# GRUB Environment Block\n" + "".join(l + "\n" for l in lines + [f"saved_entry={live}"])
open(path, "w").write(body + "#" * (1024 - len(body)))
PY
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
echo "Raw image ready: ${OUT}"
