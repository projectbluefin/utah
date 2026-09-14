#!/usr/bin/env bash
# Bake Utah's default Flatpaks and the bootc-installer bundle into the live
# squashfs. Adapted from dakota-iso: the cache is build-only; the resulting
# Flatpak repository is part of the ISO and is available offline to fisherman.
set -euo pipefail

FLATPAK_CACHE=/var/cache/flatpak-dl
INSTALLER_APP_ID=org.bootcinstaller.Installer
# #50 asks for this to point at tuna-os/bootc-installer. It does not yet,
# and the reason is a VERSIONING change, not a missing artifact:
#
#   projectbluefin/bootc-installer  semver tags ... v3.0.13 v3.0.14 v3.0.16
#   tuna-os/bootc-installer         semver tags ... v3.0.13 v3.0.14,
#                                   then date tags  v2026.09.08-2684a1d, ...
#
# The two share tag history through v3.0.14, then tuna-os switched to
# date-based tags. So v3.0.16 exists only here (verified: the flatpak asset
# 200s from projectbluefin and 404s from tuna-os at that tag), while
# tuna-os DOES publish the same org.bootcinstaller.Installer.flatpak asset at
# its date tags (verified 200 at v2026.09.14-9a9a913).
#
# Repointing therefore also means re-pinning onto a different versioning
# scheme, and #48 is open against these same lines proposing the opposite
# direction (stay here, add a sha256 pin). Which org is canonical is the open
# question in #50; whoever answers it should move INSTALLER_REPO and
# INSTALLER_VERSION together, because moving the repo alone breaks the build
# under `curl --fail`.
INSTALLER_REPO=projectbluefin/bootc-installer
FALLBACK_REPO=tuna-os/tuna-installer
BUNDLE=org.bootcinstaller.Installer.flatpak
# Pin the installer release so ISO composition is reproducible rather than
# resolving a mutable `latest` during the build. Override with
# UTAH_INSTALLER_VERSION when validating a newer installer.
INSTALLER_VERSION="${UTAH_INSTALLER_VERSION:-v3.0.16}"

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

mapfile -t apps < <(awk -F '"' '/^flatpak / {print $2}' /tmp/flatpaks-list)
flatpak install --system --noninteractive --no-related --or-update flathub "${apps[@]}"
flatpak uninstall --system --noninteractive --unused || true

mkdir -p "${FLATPAK_CACHE}"
rsync -a --delete /var/lib/flatpak/repo/ "${FLATPAK_CACHE}/repo/"
