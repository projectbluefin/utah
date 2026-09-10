#!/usr/bin/env bash
# Bake Utah's default Flatpaks and the bootc-installer bundle into the live
# squashfs. Adapted from dakota-iso: the cache is build-only; the resulting
# Flatpak repository is part of the ISO and is available offline to fisherman.
set -euo pipefail

FLATPAK_CACHE=/var/cache/flatpak-dl
INSTALLER_APP_ID=org.bootcinstaller.Installer
INSTALLER_REPO=projectbluefin/bootc-installer
FALLBACK_REPO=tuna-os/tuna-installer
BUNDLE=org.bootcinstaller.Installer.flatpak
# Pin the installer release so ISO composition is reproducible rather than
# resolving a mutable `latest` during the build. Override with
# UTAH_INSTALLER_VERSION when validating a newer installer.
INSTALLER_VERSION="${UTAH_INSTALLER_VERSION:-v3.0.16}"

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
if ! curl --retry 3 --fail --location \
    "https://github.com/${INSTALLER_REPO}/releases/download/${INSTALLER_VERSION}/${BUNDLE}" \
    -o /tmp/bootc-installer.flatpak; then
    curl --retry 3 --fail --location \
        "https://github.com/${FALLBACK_REPO}/releases/download/${INSTALLER_VERSION}/${BUNDLE}" \
        -o /tmp/bootc-installer.flatpak
fi
local_repo=/tmp/bootc-installer-repo
ostree init --repo="${local_repo}" --mode=archive-z2
flatpak build-import-bundle "${local_repo}" /tmp/bootc-installer.flatpak
rm -f /tmp/bootc-installer.flatpak
flatpak remote-add --system --no-gpg-verify installer-local "file://${local_repo}"
flatpak install --system --noninteractive installer-local "${INSTALLER_APP_ID}"
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
flatpak install --system --noninteractive --no-related --or-update flathub "${apps[@]}"

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
flatpak install --system --noninteractive --no-related --or-update \
    tuna-os com.mitchellh.ghostty
flatpak uninstall --system --noninteractive --unused || true

mkdir -p "${FLATPAK_CACHE}"
# Replacing the directory outright is what --delete was for: a stale object
# left in the cache would be seeded into the next build and never collected.
rm -rf "${FLATPAK_CACHE}/repo"
mkdir -p "${FLATPAK_CACHE}/repo"
cp -a /var/lib/flatpak/repo/. "${FLATPAK_CACHE}/repo/"
