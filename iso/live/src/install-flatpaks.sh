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

mkdir -p "${FLATPAK_CACHE}/tmp" /run/dbus
export TMPDIR="${FLATPAK_CACHE}/tmp"
dbus-daemon --system --fork --nopidfile
sleep 1

if [[ -d "${FLATPAK_CACHE}/repo/refs" ]]; then
    rsync -a --ignore-existing "${FLATPAK_CACHE}/repo/" /var/lib/flatpak/repo/ || true
fi

flatpak remote-add --system --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo

# A bundle import needs a temporary local remote in an OCI build: direct
# --bundle installs omit the deploy/active ref without flatpak-system-helper.
if ! curl --retry 3 --fail --location \
    "https://github.com/${INSTALLER_REPO}/releases/latest/download/${BUNDLE}" \
    -o /tmp/bootc-installer.flatpak; then
    curl --retry 3 --fail --location \
        "https://github.com/${FALLBACK_REPO}/releases/latest/download/${BUNDLE}" \
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

mapfile -t apps < <(awk -F '"' '/^flatpak / {print $2}' /tmp/flatpaks-list)
flatpak install --system --noninteractive --no-related --or-update flathub "${apps[@]}"
flatpak uninstall --system --noninteractive --unused || true

mkdir -p "${FLATPAK_CACHE}"
rsync -a --delete /var/lib/flatpak/repo/ "${FLATPAK_CACHE}/repo/"
