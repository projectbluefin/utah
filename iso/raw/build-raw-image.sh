#!/usr/bin/env bash
# Build the Utah raw disk image that is both the live medium and the install
# payload (GNOME OS-style install spike).
#
# Usage (root): build-raw-image.sh IMAGE OUTPUT_RAW [TARGET_IMGREF]
#
#   1. `bootc install to-disk` the image into OUTPUT_RAW -- exactly what a
#      fisherman install does at install time today, done once at build time.
#   2. Add a "Utah Live" BLS entry: the deployment's own entry with `ro
#      utah.live systemd.volatile=overlay` instead of `rw`, made the GRUB
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
install_args=(--via-loopback "/data/$(basename "${OUT}")" --filesystem btrfs --wipe
              --generic-image --target-imgref "${TARGET_IMGREF}")
volumes=(-v /dev:/dev -v /var/lib/containers:/var/lib/containers -v "${data}:/data")
if [[ -n "${UTAH_RAW_SSH_PUBKEY:-}" ]]; then
    install -m 0644 "${UTAH_RAW_SSH_PUBKEY}" "${data}/root-key.pub"
    install_args+=(--root-ssh-authorized-keys /data/root-key.pub)
fi
echo "bootc install to-disk ${IMAGE} -> ${OUT} (${SIZE}, target ${TARGET_IMGREF})"
start=$(date +%s)
podman run --rm --privileged --pid=host "${volumes[@]}" \
    --security-opt label=type:unconfined_t \
    "${IMAGE}" bootc install to-disk "${install_args[@]}"
echo "bootc install to-disk took $(( $(date +%s) - start ))s" | tee "${EVIDENCE}/install-time.txt"
rm -f "${data}/root-key.pub"

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
[[ -n "${esp}" && -n "${boot}" && -n "${root}" ]] || { echo "unexpected layout: esp=${esp} boot=${boot} root=${root}" >&2; exit 1; }
mkdir -p "${mnt}/boot" "${mnt}/efi" "${mnt}/root"
mount "${boot}" "${mnt}/boot"
mount "${esp}" "${mnt}/efi"

# --- Live entry ---------------------------------------------------------------
mapfile -t entries < <(find "${mnt}/boot/loader/entries/" -name '*.conf' ! -name "${LIVE_ENTRY}.conf")
[[ ${#entries[@]} -eq 1 ]] || { echo "expected one deployment entry, found ${#entries[@]}" >&2; exit 1; }
python3 - "${entries[0]}" "${mnt}/boot/loader/entries/${LIVE_ENTRY}.conf" "${LIVE_KARGS}" <<'PY'
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
        line = "options " + " ".join(args + ["ro", "utah.live", "systemd.volatile=overlay"] + extra)
    out.append(line)
open(dst, "w").write("\n".join(out) + "\n")
PY
cat "${entries[0]}" >"${EVIDENCE}/entry-installed.conf"
cat "${mnt}/boot/loader/entries/${LIVE_ENTRY}.conf" | tee "${EVIDENCE}/entry-live.conf"

grubenv="${mnt}/boot/grub2/grubenv"
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
for f in "${mnt}"/efi/EFI/*/grub.cfg "${mnt}"/efi/EFI/*/bootuuid.cfg "${mnt}"/boot/grub2/*.cfg; do
    [[ -f "$f" ]] && { echo "== ${f#"${mnt}"/}"; cat "$f"; }
done >"${EVIDENCE}/grub-configs.txt"

# --- CI-only diagnostics on the deployment ------------------------------------
if [[ "${UTAH_RAW_DEBUG_SSHD:-0}" == 1 ]]; then
    mount -o subvol=/ "${root}" "${mnt}/root" 2>/dev/null || mount "${root}" "${mnt}/root"
    for d in "${mnt}"/root/ostree/deploy/*/deploy/*.0; do
        install -d "$d/etc/systemd/system/graphical.target.wants"
        ln -sfn /usr/lib/systemd/system/sshd.service "$d/etc/systemd/system/graphical.target.wants/sshd.service"
        cat "$d/etc/fstab" >"${EVIDENCE}/fstab.txt" 2>/dev/null || true
        printf '%s\n' "machine-id: '$(cat "$d/etc/machine-id" 2>/dev/null)'" >>"${EVIDENCE}/fstab.txt"
    done
    find "${mnt}"/root/ostree/deploy/*/deploy -maxdepth 1 -name '*.origin' -exec cat {} + >"${EVIDENCE}/origin.txt"
    umount "${mnt}/root"
fi
sync
umount "${mnt}/efi" "${mnt}/boot"

{
    echo "apparent: $(du -h --apparent-size "${OUT}" | cut -f1)"
    echo "allocated: $(du -h "${OUT}" | cut -f1)"
} | tee "${EVIDENCE}/size.txt"
echo "Raw image ready: ${OUT}"
