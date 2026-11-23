#!/usr/bin/env bash
# Report what still holds the installer's target filesystem. Run as root in
# the live guest after the installer has failed:
#
#   ssh ... "sudo bash -s -- SINCE [FSTYPE]" < iso/scripts/target-holders.sh
#
# fisherman's finalize remounts the installed root read-only, then closes its
# LUKS mapping. When something else still uses that filesystem the remount
# fails "mount point is busy", the teardown lazily unmounts every host mount
# of it, and the close fails "Device fisherman-root is still in use" (#1467,
# tuna-os/fisherman#505). The holder outlives the installer, but luks-e2e.sh
# used to stop the VM at once and the evidence went with it.
#
# This prints metadata only -- never file contents, command lines or
# credentials:
#   - the block devices, and each open crypt mapping's open count, holders
#     and the processes that have its device node open
#   - every mount namespace that still mounts an FSTYPE filesystem, with its
#     processes and their cgroups (a service's systemd unit)
#   - every process with a file descriptor, working directory, root or memory
#     mapping on one, with the descriptor's access mode: an open writer, or an
#     open file that has been deleted, is what fails a read-only remount
#   - systemd's unit activity and the kernel log since SINCE (journalctl
#     --since syntax; empty means the whole boot)
#
# FSTYPE (default btrfs, the harness recipe's root filesystem) is how the
# target is recognised: the live media mounts no other filesystem of that
# type. Matching the mapper's device number does not work: files on btrfs
# report their subvolume's anonymous device, which is also why the
# installer's own `fuser -km /dev/mapper/...` finds nothing, and a lazily
# detached mount is in no mount table at all.
#
# Diagnostics must not stop at the first error: no `set -e`, and every probe
# tolerates a process that exits while it is being read.
set -uo pipefail
shopt -s nullglob

SINCE="${1:-}"
FSTYPE="${2:-btrfs}"
journal_window=(-b)
[[ -n "${SINCE}" ]] && journal_window+=(--since "${SINCE}")

# describe PID: "pid N (comm) cgroup", the cgroup naming a service's unit.
describe() {
    local pid="$1" comm cgroup
    comm="$(cat "/proc/${pid}/comm" 2>/dev/null)" || comm="exited"
    cgroup="$(sed -n 's/^0:://p' "/proc/${pid}/cgroup" 2>/dev/null)"
    echo "pid ${pid} (${comm}) ${cgroup:-no-cgroup}"
}

echo "=== what holds the ${FSTYPE} target, $(date -u '+%Y-%m-%d %H:%M:%S UTC') ==="

echo
echo "=== block devices ==="
lsblk -o NAME,MAJ:MIN,TYPE,FSTYPE,SIZE,MOUNTPOINTS 2>&1

echo
echo "=== open crypt mappings ==="
if command -v dmsetup >/dev/null 2>&1; then
    while read -r name _; do
        [[ -e "/dev/mapper/${name}" ]] || continue
        dmsetup info "${name}" 2>&1
        node="$(readlink -f "/dev/mapper/${name}")"
        stacked=(/sys/class/block/"$(basename "${node}")"/holders/*)
        echo "device ${node}; stacked holders: ${stacked[*]##*/}"
        if command -v fuser >/dev/null 2>&1; then
            echo "processes with ${node} open:"
            fuser -v "${node}" 2>&1 || echo "  none"
        fi
        echo
    done < <(dmsetup ls --target crypt 2>/dev/null)
else
    echo "dmsetup not installed"
fi

echo
echo "=== mount namespaces that still mount ${FSTYPE} ==="
host_ns="$(readlink /proc/1/ns/mnt 2>/dev/null)"
declare -A members=()
for link in /proc/[0-9]*/ns/mnt; do
    pid="${link#/proc/}"
    pid="${pid%%/*}"
    ns="$(readlink "${link}" 2>/dev/null)" || continue
    members["${ns}"]+=" ${pid}"
done
found=0
for ns in "${!members[@]}"; do
    read -ra pids <<< "${members[${ns}]}"
    mounts=""
    for pid in "${pids[@]}"; do
        # mountinfo: the fields after the " - " separator are type and source.
        mounts="$(awk -v t="${FSTYPE}" '{ for (i = 7; i <= NF; i++) if ($i == "-") { if ($(i + 1) == t) print; break } }' \
            "/proc/${pid}/mountinfo" 2>/dev/null)" && break
    done
    [[ -n "${mounts}" ]] || continue
    found=1
    if [[ "${ns}" == "${host_ns}" ]]; then
        echo "${ns}: the host's own namespace (${#pids[@]} processes)"
    else
        echo "${ns}: ${#pids[@]} process(es)"
        for pid in "${pids[@]}"; do echo "  $(describe "${pid}")"; done
    fi
    sed 's/^/  mountinfo: /' <<< "${mounts}"
done
(( found )) || echo "none"

echo
echo "=== processes with a file, working directory, root or mapping on ${FSTYPE} ==="
# One stat per batch: a live desktop session has tens of thousands of these.
# statfs follows each /proc link to the open file itself, so a file on a
# lazily detached mount still reports its filesystem type.
holders="$(
    printf '%s\0' /proc/[0-9]*/cwd /proc/[0-9]*/root /proc/[0-9]*/fd/* /proc/[0-9]*/map_files/* \
        | xargs -0r stat -f -L -c '%n %T' 2>/dev/null \
        | while read -r path type; do
            [[ "${type}" == "${FSTYPE}" ]] || continue
            pid="${path#/proc/}"
            pid="${pid%%/*}"
            entry="${path#/proc/"${pid}"/}"
            file="$(readlink "${path}" 2>/dev/null)" || continue
            mode=""
            case "${entry}" in
                fd/*)
                    flags="$(sed -n 's/^flags:[[:space:]]*//p' "/proc/${pid}/fdinfo/${entry#fd/}" 2>/dev/null)"
                    [[ "${flags}" =~ ^[0-7]+$ ]] || flags=0
                    case $(( 8#${flags} & 3 )) in
                        0) mode=" (read)" ;;
                        1) mode=" (write)" ;;
                        *) mode=" (read-write)" ;;
                    esac
                    ;;
                map_files/*) entry="mmap" ;;
            esac
            echo "$(describe "${pid}"): ${entry}${mode} -> ${file}"
        done | sort -u
)"
echo "${holders:-none}"

echo
echo "=== systemd unit activity${SINCE:+ since ${SINCE}} ==="
journalctl "${journal_window[@]}" _PID=1 -o short-iso-precise --no-pager 2>&1 | tail -n 1000

echo
echo "=== kernel log${SINCE:+ since ${SINCE}} ==="
journalctl "${journal_window[@]}" -k -o short-iso-precise --no-pager 2>&1 | tail -n 500
exit 0
