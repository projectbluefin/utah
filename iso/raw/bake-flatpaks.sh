#!/usr/bin/env bash
# Bake the default Flatpaks into the image-based installer's medium.
#
# Usage (root): bake-flatpaks.sh IMAGE RAW
#
# The published image stays Flatpak-free (docs/adr/0001, decision 6). The
# Flatpaks go into the medium's stateful /var instead: the shared /var's
# lib/flatpak on the raw image's root filesystem (ostree: the stateroot's
# ostree/deploy/<stateroot>/var; composefs: state/os/default/var, which every
# state/deploy/<digest>/var links to), which `bootc install` has just created.
# bootc never touches /var after the first deployment,
# so a bootc update can neither re-add nor overwrite them, and an app the user
# removes stays removed. The installer's block copy carries that /var to the
# installed disk, so first boot starts with them offline.
#
# The set is the one the ISO bakes (iso/live/src/install-flatpaks.sh): the
# apps of the image's system-flatpaks.Brewfile, converted by the same parser
# (scripts/verify-desktop-contract.py --flatpaks), installed from Flathub
# with --no-related and the listed runtimes pinned. Then `flatpak preinstall`
# adds the image's /usr/share/flatpak/preinstall.d set (Ghostty from the
# TunaOS remote, Bazaar, ...) and records it as preinstalled, so
# flatpak-preinstall.service has nothing left to fetch on the installed
# system's first boot. It runs in IMAGE with the stateroot's var/lib/flatpak
# bind-mounted at /var/lib/flatpak, so the image's remotes.d descriptors and
# flatpak version apply. setfiles then labels var/lib with the image's file
# contexts: the installed system runs SELinux enforcing. All of var/lib, not
# only var/lib/flatpak: on a fresh stateroot the `install -d` below creates
# var/lib too, from the host, with no label at all, and on first boot
# systemd (init_t) is denied add_name in the unlabeled_t /var/lib, so
# systemd-timesyncd's StateDirectory fails until start-limit-hit (Post-Testing
# E2E run 45858480299). The build fails if /var/lib is not var_lib_t.
#
# Environment: UTAH_RAW_EVIDENCE (default: raw-evidence beside RAW).
set -euo pipefail

IMAGE="${1:?image ref is required}"
RAW="$(realpath "${2:?raw image path is required}")"
EVIDENCE="${UTAH_RAW_EVIDENCE:-$(dirname "${RAW}")/raw-evidence}"
PARSER="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/scripts/verify-desktop-contract.py"

[[ ${EUID} -eq 0 ]] || { echo "run as root (loop devices, mounts)" >&2; exit 1; }
[[ -f "${PARSER}" ]] || { echo "missing ${PARSER}" >&2; exit 1; }
mkdir -p "${EVIDENCE}"
loop="$(losetup --find --show --partscan "${RAW}")"
mnt="$(mktemp -d)"
cleanup() {
    set +e
    mountpoint -q "${mnt}" && umount "${mnt}"
    losetup -d "${loop}"
    rmdir "${mnt}"
}
trap cleanup EXIT
udevadm settle
root=""
for p in "${loop}"p*; do
    [[ "$(blkid -p -s TYPE -o value "$p" || true)" == btrfs ]] && root="$p"
done
[[ -n "${root}" ]] || { echo "no btrfs root on ${RAW}" >&2; exit 1; }
mount "${root}" "${mnt}"
# The directory holding the shared var/: one ostree stateroot, or composefs's
# state/os/default.
mapfile -t stateroots < <(find "${mnt}/ostree/deploy" -mindepth 1 -maxdepth 1 -type d 2>/dev/null)
[[ -d "${mnt}/state/os/default/var" ]] && stateroots+=("${mnt}/state/os/default")
[[ ${#stateroots[@]} -eq 1 ]] || { echo "expected one stateroot, found ${#stateroots[@]}" >&2; exit 1; }
stateroot="${stateroots[0]}"
install -d "${stateroot}/var/lib/flatpak"

start=$(date +%s)
podman run --rm --privileged --network host --security-opt label=type:unconfined_t \
    -v "${stateroot}/var/lib/flatpak:/var/lib/flatpak" -v "${stateroot}:/stateroot" \
    -v "${PARSER}:/run/utah-flatpak-parser.py:ro" \
    "${IMAGE}" bash -euc '
        # Build-time only, as in iso/live/src/install-flatpaks.sh: a machine
        # id and a system bus for flatpak, a session bus for the OCI (TunaOS)
        # remote, which otherwise tries to autolaunch one and fails with
        # "Cannot autolaunch D-Bus without X11 $DISPLAY", and /var/tmp for its
        # token cache. /root is a dangling symlink in a bootc image, so the
        # caches go to /tmp.
        [[ -s /etc/machine-id ]] || systemd-machine-id-setup >/dev/null 2>&1 || dbus-uuidgen >/etc/machine-id
        mkdir -p /run/dbus /var/tmp /tmp/cache && chmod 1777 /var/tmp
        export HOME=/tmp XDG_CACHE_HOME=/tmp/cache
        dbus-daemon --system --fork --nopidfile
        DBUS_SESSION_BUS_ADDRESS="$(dbus-daemon --session --fork --print-address)"
        export DBUS_SESSION_BUS_ADDRESS
        # Flathub pulls time out now and then; a retry resumes the partial
        # pull, and --or-update keeps a retry of a finished ref a no-op.
        retry() {
            local attempt
            for attempt in 1 2 3 4 5; do
                "$@" && return 0
                [[ ${attempt} -lt 5 ]] || { echo "failed 5 times: $*" >&2; return 1; }
                sleep $(( attempt * 30 ))
            done
        }
        mapfile -t apps < <(python3 /run/utah-flatpak-parser.py --flatpaks \
            /usr/share/ublue-os/homebrew/system-flatpaks.Brewfile | grep -v "^[[:space:]]*\(#\|$\)")
        (( ${#apps[@]} )) || { echo "no Flatpaks in system-flatpaks.Brewfile" >&2; exit 1; }
        retry flatpak remote-add --system --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo
        retry flatpak install --system --noninteractive --no-related --or-update flathub "${apps[@]}"
        # Runtimes the Brewfile lists on their own (the adw-gtk3 themes) have
        # no app depending on them; pin them so --unused keeps them, here and
        # on the installed system.
        declare -A wanted=()
        for app in "${apps[@]}"; do wanted["${app}"]=1; done
        while read -r ref; do
            id="${ref%%/*}"
            [[ -z "${wanted[${id}]:-}" ]] || flatpak pin --system "runtime/${ref}"
        done < <(flatpak list --system --runtime --columns=ref)
        retry flatpak preinstall --system --noninteractive -y
        flatpak uninstall --system --noninteractive --unused
        flatpak list --system --app --columns=application | sort >/stateroot/.utah-flatpaks-apps
        flatpak list --system --runtime --columns=ref | sort >/stateroot/.utah-flatpaks-runtimes
        setfiles -F -r /stateroot /etc/selinux/targeted/contexts/files/file_contexts /stateroot/var/lib
        for d in /stateroot/var /stateroot/var/lib /stateroot/var/lib/flatpak; do
            echo "${d#/stateroot}: $(getfattr --absolute-names --only-values -n security.selinux "$d" | tr -d "\0")"
        done >/stateroot/.utah-var-labels
    '
seconds=$(( $(date +%s) - start ))
mv "${stateroot}/.utah-flatpaks-apps" "${EVIDENCE}/flatpak-apps.txt"
mv "${stateroot}/.utah-flatpaks-runtimes" "${EVIDENCE}/flatpak-runtimes.txt"
mv "${stateroot}/.utah-var-labels" "${EVIDENCE}/var-labels.txt"
cat "${EVIDENCE}/var-labels.txt"
grep -q '^/var/lib: .*:var_lib_t:' "${EVIDENCE}/var-labels.txt" \
    || { echo "/var/lib is not labeled var_lib_t (var-labels.txt above)" >&2; exit 1; }
bytes=$(du -sxb "${stateroot}/var/lib/flatpak" | cut -f1)
apps=$(wc -l <"${EVIDENCE}/flatpak-apps.txt")
[[ ${apps} -gt 0 ]] || { echo "the bake installed no apps" >&2; exit 1; }
cat "${EVIDENCE}/flatpak-apps.txt"
printf '{"apps": %s, "runtimes": %s, "bytes": %s, "seconds": %s}\n' "${apps}" \
    "$(wc -l <"${EVIDENCE}/flatpak-runtimes.txt")" "${bytes}" "${seconds}" | tee "${EVIDENCE}/flatpaks.json"
