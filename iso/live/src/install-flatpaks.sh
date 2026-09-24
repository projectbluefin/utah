#!/usr/bin/env bash
# Bake Utah's default Flatpaks and the bootc-installer bundle into the live
# squashfs. Adapted from dakota-iso: the cache is build-only; the resulting
# Flatpak repository is part of the ISO and is available offline to fisherman.
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
# so a retry re-fetches only what is still missing, and every install below
# passes --or-update, which makes a retry a no-op for refs already complete.
retry_flatpak() {
    local attempt
    for attempt in 1 2 3; do
        if flatpak "$@"; then
            return 0
        fi
        echo "flatpak $1 attempt ${attempt} of 3 failed" >&2
        if (( attempt < 3 )); then
            sleep $(( attempt * 15 ))
        fi
    done
    echo "ERROR: flatpak $1 failed after 3 attempts: $*" >&2
    return 1
}

FLATPAK_CACHE=/var/cache/flatpak-dl
INSTALLER_APP_ID=org.bootcinstaller.Installer
INSTALLER_REPO=tuna-os/bootc-installer
BUNDLE=org.bootcinstaller.Installer.flatpak
# Pin the installer release so ISO composition is reproducible rather than
# resolving a mutable `latest` during the build. Override with
# UTAH_INSTALLER_VERSION when validating a newer installer.
INSTALLER_VERSION="${UTAH_INSTALLER_VERSION:-v2026.09.19-cee9ba29}"
# The bundle is installed system-wide with --no-gpg-verify below, so the
# version pin alone is the whole trust story. Pin its SHA-256 the same way
# the Containerfile pins UUPD_SHA256, and verify before import. Version and
# digest move together; override with UTAH_INSTALLER_SHA256 when validating
# a newer installer.
INSTALLER_SHA256="${UTAH_INSTALLER_SHA256:-ebd661e554523957a05e6dba038512369d232adb0512b63233512db4cc012941}"

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
fi

flatpak remote-add --system --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo

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
# --or-update for the same reason the Flathub installs below carry it: a retry
# must be a no-op for a ref that already completed. Without it, an attempt
# that installed the app but still exited nonzero would make attempts 2 and 3
# fail with "already installed", turning a flaky success into a hard failure.
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

# /tmp/flatpaks-list already holds bare application ids: the Containerfile
# converts the Brewfile before copying it in, so the contract stays the single
# source of truth. Parsing it as Brewfile syntax a second time matched nothing
# and left the array empty, and an empty array makes flatpak read the remote
# name as the thing to install:
#   error: No remote refs found for 'flathub'
mapfile -t apps < <(grep -v '^[[:space:]]*#' /tmp/flatpaks-list | grep -v '^[[:space:]]*$')
if (( ${#apps[@]} == 0 )); then
    echo "No flatpaks listed in /tmp/flatpaks-list; the Brewfile conversion is broken" >&2
    exit 1
fi
retry_flatpak install --system --noninteractive --no-related --or-update flathub "${apps[@]}"

# Ghostty, from the TunaOS OCI remote.
#
# Utah ships no terminal emulator at all otherwise. Bluefin's own image test
# asserts ptyxis, but ptyxis is not in Bluefin's package contract because
# Fedora's base image carries it -- and Hummingbird's does not, nor does it
# package ptyxis, vte291 or gnome-console, so there is nothing to install.
# Until this factory builds a terminal, the flatpak is the terminal.
#
# Kept out of the Brewfile-derived list on purpose: that list is the parity
# contract with Bluefin and verify-desktop-contract compares it byte for byte.
# This is Utah's own addition and does not belong in it.
flatpak remote-add --system --if-not-exists tuna-os \
    https://tunaos.org/flatpak/tuna-os.flatpakrepo
retry_flatpak install --system --noninteractive --no-related --or-update \
    tuna-os com.mitchellh.ghostty
# `uninstall --unused` removes every runtime that no installed app depends on,
# and the Brewfile lists two of exactly that kind: the adw-gtk3 GTK3 themes.
# Nothing requires them, so they were stripped from the ISO and the offline
# install check failed on every flavor (post-testing-e2e run 36047291319):
#   FAIL: default Flatpak(s) missing on the installed, network-isolated
#   system: org.gtk.Gtk3theme.adw-gtk3 org.gtk.Gtk3theme.adw-gtk3-dark
# Pin each listed runtime first; --unused never removes a pinned ref. The pin
# is part of /var/lib/flatpak, so it also reaches the installed system and
# keeps later `--unused` cleanups there from removing the themes too.
declare -A wanted=()
for app in "${apps[@]}"; do wanted["${app}"]=1; done
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
