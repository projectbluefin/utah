#!/usr/bin/env bash
# Shrink the image-based installer's raw medium to its content.
#
# Usage (root): shrink-raw-image.sh IMAGE RAW
#
# bootc installs into a sparse file sized for headroom (UTAH_RAW_SIZE). The
# medium only has to hold what is on it: the live session writes to a tmpfs
# overlay, and OS-Installer's install.sh grows the root on the target
# (systemd-repart gives root the rest of the disk, then
# `btrfs filesystem resize max`). So:
#
#   1. record what takes the space (size-breakdown.txt), so duplicate copies
#      and leftovers show up in the evidence;
#   2. fstrim every filesystem, which punches holes in the file for free blocks;
#   3. compact the btrfs root (full balance) and shrink it to its allocated
#      chunks plus UTAH_RAW_SHRINK_MARGIN_MIB (default 256), stepping up by
#      256 MiB while btrfs refuses;
#   4. cut the root partition and the file right after it (medium.py
#      shrink-plan), then write the partition table again for the new size
#      (medium.py shrunk-dump): a truncated file has no backup GPT, and the
#      primary header's last usable LBA would point past the end, which makes
#      Linux ignore the whole table (spike-to-track run 44874576635: no root
#      device in the initrd).
#
# btrfs-progs runs from IMAGE, like the mkfs in build-raw-image.sh, so the
# tools match the kernel and the installed system.
#
# Environment: UTAH_RAW_EVIDENCE (default: raw-evidence beside RAW).
set -euo pipefail

IMAGE="${1:?image ref is required}"
RAW="$(realpath "${2:?raw image path is required}")"
EVIDENCE="${UTAH_RAW_EVIDENCE:-$(dirname "${RAW}")/raw-evidence}"
MARGIN_MIB="${UTAH_RAW_SHRINK_MARGIN_MIB:-256}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

[[ ${EUID} -eq 0 ]] || { echo "run as root (loop devices, mounts)" >&2; exit 1; }
mkdir -p "${EVIDENCE}"
before_apparent=$(du -b --apparent-size "${RAW}" | cut -f1)
before_allocated=$(( $(du -k "${RAW}" | cut -f1) * 1024 ))

loop="$(losetup --find --show --partscan "${RAW}")"
trap 'losetup -d "${loop}" 2>/dev/null || true' EXIT
udevadm settle
sfdisk --json "${loop}" >"${EVIDENCE}/partitions-before-shrink.json"
root="$(python3 - "${EVIDENCE}/partitions-before-shrink.json" <<'PY'
import json, sys
parts = json.load(open(sys.argv[1]))["partitiontable"]["partitions"]
print(max(parts, key=lambda p: int(p["start"]))["node"])
PY
)"

# Prints the root filesystem's size on its device, in bytes, after shrinking.
fs_bytes="$(podman run --rm --privileged -v /dev:/dev -e LOOP="${loop}" -e ROOT="${root}" \
    -e MARGIN="$(( MARGIN_MIB * 1024 * 1024 ))" --security-opt label=type:unconfined_t \
    "${IMAGE}" bash -euc '
        report() { echo "$@" >&2; }
        [[ "$(blkid -p -s TYPE -o value "${ROOT}")" == btrfs ]] || { report "root ${ROOT} is not btrfs"; exit 1; }
        mkdir -p /m /p
        mount "${ROOT}" /m
        {
            echo "== used per top-level directory of the root filesystem (du -sx, MiB)"
            du -sxm /m/* /m/ostree/* /m/ostree/deploy/*/var/* /m/ostree/deploy/*/var/lib/* 2>/dev/null | sort -rn | head -40
            echo "== btrfs filesystem du (shared extents are counted once in Set shared)"
            btrfs filesystem du -s /m/ostree/repo /m/ostree/deploy 2>/dev/null || true
            echo "== btrfs filesystem usage"
            btrfs filesystem usage -b /m
            for p in "${LOOP}"p*; do
                [[ "${p}" == "${ROOT}" ]] && continue
                t="$(blkid -p -s TYPE -o value "${p}" || true)"
                [[ "${t}" == vfat || "${t}" == ext4 ]] || continue
                mount "${p}" /p
                echo "== ${p} (${t}) used"; df -B1 --output=used,size /p | tail -1
                du -sxm /p/* 2>/dev/null | sort -rn | head -10
                fstrim -v /p
                umount /p
            done
        } >&2
        # A full balance packs data into as few chunks as possible, so the
        # allocation (not btrfs min-dev-size, which ran ~4 GB high in run
        # 44874576635) bounds the resize; resize moves chunks above the cut.
        btrfs balance start -dusage=100 -musage=100 /m >&2
        fstrim -v /m >&2
        dev=$(btrfs filesystem usage -b /m | awk "/Device size:/ {print \$3}")
        alloc=$(btrfs filesystem usage -b /m | awk "/Device allocated:/ {print \$3}")
        target=$(( (alloc + MARGIN + 1048575) / 1048576 * 1048576 ))
        report "btrfs allocated ${alloc} of ${dev}; shrinking to ${target}"
        until btrfs filesystem resize "${target}" /m >&2; do
            target=$(( target + 268435456 ))
            (( target < dev )) || { report "cannot shrink below ${dev}"; exit 1; }
            report "retrying at ${target}"
        done
        umount /m
        btrfs inspect-internal dump-super "${ROOT}" | awk "\$1 == \"dev_item.total_bytes\" {print \$2}"
    ' 2>"${EVIDENCE}/size-breakdown.txt")"
cat "${EVIDENCE}/size-breakdown.txt"
[[ "${fs_bytes}" =~ ^[0-9]+$ ]] || { echo "could not read the shrunk filesystem size: '${fs_bytes}'" >&2; exit 1; }
losetup -d "${loop}"
trap - EXIT

read -r partno sectors image_bytes < <(python3 "${HERE}/medium.py" shrink-plan \
    "${EVIDENCE}/partitions-before-shrink.json" "${fs_bytes}")
echo "root filesystem ${fs_bytes} bytes: partition ${partno} -> ${sectors} sectors, image -> ${image_bytes} bytes"
sfdisk --dump "${RAW}" >"${EVIDENCE}/partitions-before-shrink.dump"
python3 "${HERE}/medium.py" shrunk-dump "${EVIDENCE}/partitions-before-shrink.dump" "${partno}" "${sectors}" \
    >"${EVIDENCE}/partitions-shrunk.dump"
truncate -s "${image_bytes}" "${RAW}"
sfdisk --quiet --no-reread --wipe never --wipe-partitions never "${RAW}" <"${EVIDENCE}/partitions-shrunk.dump"
sfdisk --verify "${RAW}"
sfdisk --dump "${RAW}" | tee "${EVIDENCE}/partitions.txt"
grep -qx 'label: gpt' "${EVIDENCE}/partitions.txt" || { echo "the shrunk medium lost its GPT" >&2; exit 1; }

after_apparent=$(du -b --apparent-size "${RAW}" | cut -f1)
after_allocated=$(( $(du -k "${RAW}" | cut -f1) * 1024 ))
printf '{"before_apparent_bytes": %s, "before_allocated_bytes": %s, "apparent_bytes": %s, "allocated_bytes": %s, "root_fs_bytes": %s}\n' \
    "${before_apparent}" "${before_allocated}" "${after_apparent}" "${after_allocated}" "${fs_bytes}" \
    | tee "${EVIDENCE}/shrink.json"
