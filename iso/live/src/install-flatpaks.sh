#!/usr/bin/env bash
# Bake Utah's default Flatpaks and the bootc-installer bundle into the live
# squashfs. Adapted from dakota-iso: the cache is build-only; the resulting
# Flatpak repository is part of the ISO and is available offline to fisherman.
#
# Utah's default Flatpaks are declared in flatpak's standard preinstall.d, and
# those declarations ship in the Utah image itself: configure-services.sh
# generates brewfile.preinstall from the Bluefin parity Brewfile, and
# bazaar.preinstall and ghostty.preinstall are in system_files. This script only
# runs `flatpak preinstall`, so the ISO bakes exactly the declared set, and
# flatpak-preinstall.service, which reads the same entries on first boot of a
# non-ISO install, installs the same set.
set -euo pipefail

# Flathub pulls are the largest network operation in the whole ISO build --
# Firefox alone is ~200 MB -- and one timed-out object fails the compose, which
# fails the flavor's entire end-to-end run. It is not hypothetical: run
# 35432516418 lost utah-gaming to
#
#   Failed to install org.mozilla.firefox: While pulling
#   app/org.mozilla.firefox/x86_64/stable from remote flathub: While fetching
#   .../88680ed7...commitmeta: [28] Timeout was reached
#
# after 3m44s, with nothing wrong in the image. The curl above already retries
# for the same reason. flatpak resumes a partial pull from the local repository,
# so a retry re-fetches only what is still missing. The installer install
# carries --or-update (idempotent); the default-flatpaks path uses
# `flatpak preinstall`, whose preinstalled marks make a retry a no-op too --
# but preinstall exits 0 on a flaked ref, so that path is retried on its own
# verification result rather than through retry_flatpak (see the loop below).
# 3 attempts stopped being enough: post-testing-e2e run 36230660725 lost
# utah to dl.flathub.org [28] timeouts on all 3 attempts spread over
# ~20 minutes (thunderbird, then org.gnome.Platform), with nothing wrong
# in the image. 5 attempts at ~6 minutes each plus backoff covers a
# ~35-minute outage window; flatpak resumes partial pulls, and both the
# --or-update installer and the preinstall marks keep every retry a no-op.
retry_flatpak() {
    local attempt max_attempts=5
    for (( attempt = 1; attempt <= max_attempts; attempt++ )); do
        if flatpak "$@"; then
            return 0
        fi
        echo "flatpak $1 attempt ${attempt} of ${max_attempts} failed" >&2
        if (( attempt < max_attempts )); then
            sleep $(( attempt * 30 ))
        fi
    done
    echo "ERROR: flatpak $1 failed after ${max_attempts} attempts: $*" >&2
    return 1
}

FLATPAK_CACHE=/var/cache/flatpak-dl
# Utah's default Flatpaks, as the image declares them (see the header).
PREINSTALL_DIR=/usr/share/flatpak/preinstall.d
INSTALLER_APP_ID=org.bootcinstaller.Installer
INSTALLER_REPO=tuna-os/bootc-installer
BUNDLE=org.bootcinstaller.Installer.flatpak
# Pin the installer release so ISO composition is reproducible rather than
# resolving a mutable `latest` during the build. Override with
# UTAH_INSTALLER_VERSION when validating a newer installer.
INSTALLER_VERSION="${UTAH_INSTALLER_VERSION:-v2026.09.25-51f8cfe6}"
# The bundle is installed system-wide with --no-gpg-verify below, so the
# version pin alone is the whole trust story. Pin its SHA-256 the same way
# the Containerfile pins UUPD_SHA256, and verify before import. Version and
# digest move together; override with UTAH_INSTALLER_SHA256 when validating
# a newer installer.
INSTALLER_SHA256="${UTAH_INSTALLER_SHA256:-a303514765c3ba8c33e71e5361f36cf4bad906d46d473818286527891c40c5b5}"

mkdir -p "${FLATPAK_CACHE}/tmp" /run/dbus
export TMPDIR="${FLATPAK_CACHE}/tmp"
# /root is a symlink to /var/roothome in a bootc image and the target does not
# exist during a container build, so mkdir -p /root/... fails outright. Point
# the cache somewhere writable instead; this only silences dconf's warnings.
export XDG_CACHE_HOME="${FLATPAK_CACHE}/cache"
mkdir -p "${XDG_CACHE_HOME}"
# An OCI flatpak remote makes flatpak spawn a session bus of its own, and a bus
# refuses to start without a machine id -- which a container image does not
# have. Flathub installs never needed one; the TunaOS remote does:
#   Cannot spawn a message bus without a machine-id
# The id is build-time only; systemd regenerates a real one on first boot.
if [[ ! -s /etc/machine-id ]]; then
    systemd-machine-id-setup >/dev/null 2>&1 || dbus-uuidgen > /etc/machine-id
fi
dbus-daemon --system --fork --nopidfile
# ...and a session bus. Installing from an OCI remote reaches for one and,
# finding none, tries to autolaunch it:
#   error: Cannot autolaunch D-Bus without X11 $DISPLAY
# Flathub's ostree remotes never ask for it, which is why this only appeared
# when the TunaOS remote was added.
if [[ -z "${DBUS_SESSION_BUS_ADDRESS:-}" ]]; then
    DBUS_SESSION_BUS_ADDRESS="$(dbus-daemon --session --fork --print-address)"
    export DBUS_SESSION_BUS_ADDRESS
fi
sleep 1

if [[ -d "${FLATPAK_CACHE}/repo/refs" ]]; then
    # cp, not rsync: rsync is in neither Hummingbird nor the factory, so it
    # cannot be installed into the live layer. -n keeps the seed
    # non-destructive, which is all --ignore-existing was doing.
    cp -a -n "${FLATPAK_CACHE}/repo/." /var/lib/flatpak/repo/ || true
    # The cache is a copy of the last build's whole repo, config included, and
    # that config records every ref the last build preinstalled. Seeded as-is,
    # `flatpak preinstall` below treats the declared set as already handled and
    # installs nothing, so check_missing fails every attempt. Only the objects
    # are meant to carry over.
    if [[ -f /var/lib/flatpak/repo/config ]]; then
        ostree config --repo=/var/lib/flatpak/repo unset core.xa.preinstalled
    fi
fi

# remote-add fetches the .flatpakrepo over the network, so it flakes like the
# pulls do: production-iso for utah in run 36077426925 died on
#   Can't load uri https://dl.flathub.org/repo/flathub.flatpakrepo: [28] Timeout
# after every E2E flavor had passed. --if-not-exists keeps a retry a no-op once
# one attempt succeeds.
retry_flatpak remote-add --system --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo
# Ghostty resolves from the TunaOS remote. The image vendors that remote's
# descriptor at /etc/flatpak/remotes.d/tuna-os.flatpakrepo; register it from
# the vendored file so the bake never fetches its remote configuration.
# --if-not-exists keeps this a no-op if flatpak already imported remotes.d.
retry_flatpak remote-add --system --if-not-exists tuna-os \
    /etc/flatpak/remotes.d/tuna-os.flatpakrepo

# A bundle import needs a temporary local remote in an OCI build: direct
# --bundle installs omit the deploy/active ref without flatpak-system-helper.
# The download comes only from INSTALLER_REPO, tuna-os/bootc-installer -- the
# sole upstream for bootc-installer and its fisherman backend now that
# projectbluefin/bootc-installer and projectbluefin/fisherman are archived.
# An earlier version of this script also fell back to tuna-os/tuna-installer
# on failure, which would have let a release in a different org substitute
# the installer every Utah ISO ships; that fallback was removed instead.
curl --retry 3 --fail --location \
    "https://github.com/${INSTALLER_REPO}/releases/download/${INSTALLER_VERSION}/${BUNDLE}" \
    -o /tmp/bootc-installer.flatpak
echo "${INSTALLER_SHA256}  /tmp/bootc-installer.flatpak" | sha256sum --check --strict
local_repo=/tmp/bootc-installer-repo
ostree init --repo="${local_repo}" --mode=archive-z2
flatpak build-import-bundle "${local_repo}" /tmp/bootc-installer.flatpak
rm -f /tmp/bootc-installer.flatpak
flatpak remote-add --system --no-gpg-verify installer-local "file://${local_repo}"
# --or-update makes a retry harmless for an installer ref already deployed.
# Without it, an install that deployed the app but exited nonzero would make
# every later attempt fail with "already installed".
retry_flatpak install --system --noninteractive --or-update installer-local \
    "${INSTALLER_APP_ID}"
flatpak remote-delete --system --force installer-local || true
rm -rf "${local_repo}"

# Recreate the active deployment link normally written by flatpak-system-helper.
for branch in /var/lib/flatpak/app/${INSTALLER_APP_ID}/x86_64/*; do
    [[ -d "${branch}" ]] || continue
    if [[ ! -L "${branch}/active" ]]; then
        deployment="$(find "${branch}" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' | head -1)"
        [[ -n "${deployment}" ]] && ln -sfn "${deployment}" "${branch}/active"
    fi
done
flatpak override --system --filesystem=/etc:ro "${INSTALLER_APP_ID}"

# Install everything the image declares in preinstall.d, the same entries
# flatpak-preinstall.service reads on a non-ISO install's first boot.
# --no-related keeps locale extensions out of the squashfs, as the former
# hand-maintained install did. flatpak marks a ref preinstalled only once it
# deploys, so a retry after a timed-out pull resumes with what is still missing.
mapfile -t declared < <(sed -n 's/^\[Flatpak Preinstall \(.*\)\]$/\1/p' \
    "${PREINSTALL_DIR}"/*.preinstall | sort -u)
if (( ${#declared[@]} == 0 )); then
    echo "No Flatpaks declared in ${PREINSTALL_DIR}; the image's preinstall.d is broken" >&2
    exit 1
fi
#
# `flatpak preinstall` skips a ref no remote resolves -- a wrong Branch, a
# CollectionID no configured remote carries, a remote whose summary or OCI
# index fetch timed out -- and still exits 0 (a g_warning, then on to the next
# entry). That is how bazaar.preinstall's CollectionID=org.flathub.Stable went
# unnoticed. A missing default Flatpak is a build failure, never a silent skip
# -- but it is also the flake class retry_flatpak exists for, so a zero exit
# from preinstall is not the thing worth retrying: the verification below is.
# Install and verification therefore loop together. Retrying preinstall alone
# would have turned a flaked remote-metadata fetch into one unretried failure,
# which is exactly how the Ghostty pull from tuna-os used to be covered.
missing=()
check_missing() {
    local id
    missing=()
    for id in "${declared[@]}"; do
        flatpak info --system "${id}" >/dev/null 2>&1 || missing+=("${id}")
    done
}
max_attempts=5
for (( attempt = 1; attempt <= max_attempts; attempt++ )); do
    flatpak preinstall --system --noninteractive -y --no-related \
        || echo "flatpak preinstall attempt ${attempt} of ${max_attempts} failed" >&2
    check_missing
    (( ${#missing[@]} == 0 )) && break
    echo "declared but not installed after attempt ${attempt} of ${max_attempts}: ${missing[*]}" >&2
    if (( attempt < max_attempts )); then
        sleep $(( attempt * 30 ))
    fi
done
if (( ${#missing[@]} > 0 )); then
    echo "ERROR: declared in ${PREINSTALL_DIR} but not installed after" \
        "${max_attempts} attempts: ${missing[*]}" >&2
    exit 1
fi

# `uninstall --unused` removes every runtime that no installed app depends on,
# and the Brewfile lists two of exactly that kind: the adw-gtk3 GTK3 themes.
# Nothing requires them, so they were stripped from the ISO and the offline
# install check failed on every flavor (post-testing-e2e run 36047291319):
#   FAIL: default Flatpak(s) missing on the installed, network-isolated
#   system: org.gtk.Gtk3theme.adw-gtk3 org.gtk.Gtk3theme.adw-gtk3-dark
# Pin each declared runtime first; --unused never removes a pinned ref. The pin
# is part of /var/lib/flatpak, so it also reaches the installed system and
# keeps later `--unused` cleanups there from removing the themes too.
declare -A wanted=()
for id in "${declared[@]}"; do wanted["${id}"]=1; done
while read -r ref; do
    id="${ref#runtime/}"; id="${id%%/*}"
    if [[ -n "${wanted[${id}]:-}" ]]; then
        flatpak pin --system "${ref}"
    fi
done < <(flatpak list --system --runtime --columns=ref | sed 's|^|runtime/|')
flatpak uninstall --system --noninteractive --unused || true

mkdir -p "${FLATPAK_CACHE}"
# Replacing the directory outright is what --delete was for: a stale object
# left in the cache would be seeded into the next build and never collected.
rm -rf "${FLATPAK_CACHE}/repo"
mkdir -p "${FLATPAK_CACHE}/repo"
cp -a /var/lib/flatpak/repo/. "${FLATPAK_CACHE}/repo/"
