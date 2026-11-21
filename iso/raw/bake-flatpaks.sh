#!/usr/bin/env bash
# Bake the default Flatpaks into the image-based installer's medium.
#
# Usage (root): bake-flatpaks.sh IMAGE RAW
#
# The published image stays Flatpak-free (docs/adr/0001, decision 6). The
# Flatpaks go into the medium's stateful /var instead: the stateroot's
# var/lib/flatpak on the raw image's root filesystem, which `bootc install`
# has just created. ostree/bootc never touch /var after the first deployment,
# so a bootc update can neither re-add nor overwrite them, and an app the user
# removes stays removed. OS-Installer's block copy carries that /var to the
# installed disk, so first boot starts with them offline.
#
# The set is the image's own: `flatpak preinstall` reads the image's
# /usr/share/flatpak/preinstall.d (common's defaults plus Utah's additions;
# the list the ISO bakes, #257) and records the refs as preinstalled, the same
# state flatpak-preinstall.service leaves on a fisherman install. It runs in
# IMAGE with the stateroot's var/lib/flatpak bind-mounted at /var/lib/flatpak,
# so the image's remotes.d descriptors (Flathub with its collection ID) and
# flatpak version apply. setfiles then labels the new tree with the image's
# file contexts: the installed system runs SELinux enforcing.
#
# Environment: UTAH_RAW_EVIDENCE (default: raw-evidence beside RAW).
set -euo pipefail

IMAGE="${1:?image ref is required}"
RAW="$(realpath "${2:?raw image path is required}")"
EVIDENCE="${UTAH_RAW_EVIDENCE:-$(dirname "${RAW}")/raw-evidence}"

[[ ${EUID} -eq 0 ]] || { echo "run as root (loop devices, mounts)" >&2; exit 1; }
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
mapfile -t stateroots < <(find "${mnt}/ostree/deploy" -mindepth 1 -maxdepth 1 -type d)
[[ ${#stateroots[@]} -eq 1 ]] || { echo "expected one stateroot, found ${#stateroots[@]}" >&2; exit 1; }
stateroot="${stateroots[0]}"
install -d "${stateroot}/var/lib/flatpak"

start=$(date +%s)
podman run --rm --privileged --network host --security-opt label=type:unconfined_t \
    -v "${stateroot}/var/lib/flatpak:/var/lib/flatpak" -v "${stateroot}:/stateroot" \
    "${IMAGE}" bash -euc '
        # flatpak wants a system bus and a machine id; both are build-time only.
        [[ -s /etc/machine-id ]] || systemd-machine-id-setup >/dev/null 2>&1 || dbus-uuidgen >/etc/machine-id
        mkdir -p /run/dbus /var/tmp && chmod 1777 /var/tmp
        dbus-daemon --system --fork --nopidfile
        # Flathub pulls time out now and then (iso/live/src/install-flatpaks.sh);
        # a retry resumes the partial pull.
        for attempt in 1 2 3 4 5; do
            flatpak preinstall --system --noninteractive -y && break
            [[ ${attempt} -lt 5 ]] || { echo "flatpak preinstall failed 5 times" >&2; exit 1; }
            sleep $(( attempt * 30 ))
        done
        flatpak list --system --app --columns=application | sort >/stateroot/.utah-flatpaks-apps
        flatpak list --system --runtime --columns=ref | sort >/stateroot/.utah-flatpaks-runtimes
        setfiles -F -r /stateroot /etc/selinux/targeted/contexts/files/file_contexts /stateroot/var/lib/flatpak
    '
seconds=$(( $(date +%s) - start ))
mv "${stateroot}/.utah-flatpaks-apps" "${EVIDENCE}/flatpak-apps.txt"
mv "${stateroot}/.utah-flatpaks-runtimes" "${EVIDENCE}/flatpak-runtimes.txt"
bytes=$(du -sxb "${stateroot}/var/lib/flatpak" | cut -f1)
apps=$(wc -l <"${EVIDENCE}/flatpak-apps.txt")
[[ ${apps} -gt 0 ]] || { echo "flatpak preinstall installed no apps" >&2; exit 1; }
cat "${EVIDENCE}/flatpak-apps.txt"
printf '{"apps": %s, "runtimes": %s, "bytes": %s, "seconds": %s}\n' "${apps}" \
    "$(wc -l <"${EVIDENCE}/flatpak-runtimes.txt")" "${bytes}" "${seconds}" | tee "${EVIDENCE}/flatpaks.json"
