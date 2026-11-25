#!/usr/bin/env bash
# Shrink the image-based installer's raw medium to its content.
#
# Usage (root): shrink-raw-image.sh IMAGE RAW
#
# bootc installs into a sparse file sized for headroom (UTAH_RAW_SIZE). The
# medium only has to hold what is on it: the live session writes to a tmpfs
# overlay, and utah-sysinstall-post grows the root on the target
# (systemd-repart gives root the rest of the disk, then
# `btrfs filesystem resize max`). So:
#
#   1. record what takes the space (size-breakdown.txt), so duplicate copies
#      and leftovers show up in the evidence;
#   2. fstrim every filesystem, which punches holes in the file for free blocks;
#   3. with UTAH_RAW_BTRFS_COMPRESS set (e.g. zstd:3), rewrite every file on
#      the btrfs root compressed, Flatpaks included; then compact the root
#      (full balance into UTAH_RAW_SHRINK_CHUNK_MIB
#      chunks, default 256, the kernel minimum) and shrink it to its
#      allocated chunks plus UTAH_RAW_SHRINK_MARGIN_MIB (default 256),
#      stepping up by 256 MiB while btrfs refuses, then trim the root again;
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
CHUNK_MIB="${UTAH_RAW_SHRINK_CHUNK_MIB:-256}"
COMPRESS="${UTAH_RAW_BTRFS_COMPRESS:-}"
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
    -e MARGIN="$(( MARGIN_MIB * 1024 * 1024 ))" -e CHUNK="$(( CHUNK_MIB * 1024 * 1024 ))" -e COMPRESS="${COMPRESS}" \
    --security-opt label=type:unconfined_t \
    "${IMAGE}" bash -euc '
        report() { echo "$@" >&2; }
        [[ "$(blkid -p -s TYPE -o value "${ROOT}")" == btrfs ]] || { report "root ${ROOT} is not btrfs"; exit 1; }
        mkdir -p /m /p
        mount "${ROOT}" /m
        {
            echo "== used per top-level directory of the root filesystem (du -sx, MiB)"
            du -sxm /m/* /m/ostree/* /m/ostree/deploy/*/var/* /m/ostree/deploy/*/var/lib/* \
                /m/composefs/* /m/state/* /m/state/os/default/var/* /m/state/os/default/var/lib/* 2>/dev/null | sort -rn | head -40
            echo "== btrfs filesystem du (shared extents are counted once in Set shared)"
            btrfs filesystem du -s /m/ostree/repo /m/ostree/deploy /m/composefs /m/state 2>/dev/null || true
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
        # ostree and flatpak fallocate each object before writing it, and
        # btrfs never compresses writes into preallocated extents: mounting
        # the root compress-force=zstd:3 while bootc wrote it left run
        # 44884511244 at 6.54 GB used, the same as uncompressed. Rewriting
        # the files compresses them (6.19 GB to 3.08 GB in 3 minutes locally),
        # and doing it here, after bake-flatpaks.sh, covers the Flatpaks too.
        # Defrag compresses at the algorithm default level (zstd: 3) on
        # kernels before 6.15, which reject -L. It also rewrites the composefs
        # fs-verity objects: the data is the same, so the Merkle tree still
        # matches and the verity flag stays (checked below, and by reading
        # every object on the installed disk in raw_e2e.py). Reading verity
        # data from compressed extents is what the Utah kernel fix is for (#1379).
        if [[ -n "${COMPRESS}" ]]; then
            start=$(date +%s)
            btrfs filesystem defragment -r -c"${COMPRESS%%:*}" /m >&2
            sync -f /m
            report "== compressed with ${COMPRESS%%:*} in $(( $(date +%s) - start ))s"
            btrfs filesystem usage -b /m >&2
            if [[ -d /m/composefs/objects ]]; then
                objects=$(find /m/composefs/objects -type f | wc -l)
                verity=$(find /m/composefs/objects -type f -print0 | xargs -0r lsattr 2>/dev/null | awk "\$1 ~ /V/" | wc -l)
                report "composefs objects with fs-verity after compression: ${verity} of ${objects}"
                (( verity == objects )) || { report "compression dropped fs-verity"; exit 1; }
            fi
        fi
        # A full balance packs data into as few chunks as possible, so the
        # allocation (not btrfs min-dev-size, which ran ~4 GB high in run
        # 44874576635) bounds the resize; resize moves chunks above the cut.
        # At the default 1 GiB data and 512 MiB DUP metadata chunks, the
        # balance packs nothing: the root still allocated 9.2 GB for 6.9 GB
        # used (run 44880690120), and a local 2 GB small-file test stayed at
        # 63% data usage. Rebalanced into 256 MiB chunks (the kernel minimum;
        # the setting lasts for this mount only and does not reach the
        # installed system) the same test packed to 94%.
        uuid=$(blkid -p -s UUID -o value "${ROOT}")
        chunks() {
            for kind in data metadata; do
                echo "${CHUNK}" >"/sys/fs/btrfs/${uuid}/allocation/${kind}/chunk_size"
                report "${kind} chunk size $(cat "/sys/fs/btrfs/${uuid}/allocation/${kind}/chunk_size")"
            done
        }
        chunks
        btrfs balance start -dusage=100 -musage=100 /m >&2
        fstrim -v /m >&2
        dev=$(btrfs filesystem usage -b /m | awk "/Device size:/ {print \$3}")
        alloc=$(btrfs filesystem usage -b /m | awk "/Device allocated:/ {print \$3}")
        target=$(( (alloc + MARGIN + 1048575) / 1048576 * 1048576 ))
        report "btrfs allocated ${alloc} of ${dev}; shrinking to ${target}"
        # On composefs the first resize usually runs out of space: fs-verity
        # keeps the Merkle tree of every object in btrfs metadata (514 MB of
        # metadata against 215 MB on ostree, no-Flatpak media of run
        # 44910214722 and 44897756832), the balance packs it nearly full, and
        # relocating a metadata chunk then needs a new DUP chunk below the
        # cut. A local copy of the composefs objects needed 1.25 GiB over the
        # allocation instead of 256 MiB.
        until btrfs filesystem resize "${target}" /m >&2; do
            # A resize that runs out of space can abort its transaction and
            # leave the filesystem read-only (run 44917186176: ENOSPC, then
            # every retry "Read-only file system"). The last commit is intact,
            # so remount (the chunk size setting is per mount) and go on.
            umount /m
            mount "${ROOT}" /m
            chunks
            target=$(( target + 268435456 ))
            (( target < dev )) || { report "cannot shrink below ${dev}"; exit 1; }
            report "retrying at ${target}"
        done
        # Trim again: chunks the resize emptied below the cut keep their old
        # data in the file until trimmed (the no-Flatpak composefs medium of
        # run 44910214722 was about 0.9 GB more allocated than its content).
        fstrim -v /m >&2
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
