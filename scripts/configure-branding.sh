#!/usr/bin/bash
# Apply Utah's OS identity to the Hummingbird base after all packages and
# system-files overlays are present. Utah consumes Bluefin's desktop assets and
# defaults, but remains a distinct, supportable Project Bluefin variant.

set -eoux pipefail

IMAGE_PRETTY_NAME="Utah"
IMAGE_LIKE="fedora"
IMAGE_NAME="${IMAGE_NAME:-utah}"
IMAGE_VENDOR="${IMAGE_VENDOR:-projectbluefin}"
IMAGE_FLAVOR="${IMAGE_FLAVOR:-main}"
VERSION="${VERSION:-testing}"
SHA_HEAD_SHORT="${SHA_HEAD_SHORT:-unknown}"
BASE_IMAGE_NAME="${BASE_IMAGE_NAME:-hummingbird}"
FEDORA_MAJOR_VERSION="${FEDORA_MAJOR_VERSION:-$(rpm -E %fedora)}"
UBLUE_IMAGE_TAG="${UBLUE_IMAGE_TAG:-${VERSION}}"
IMAGE_INFO="/usr/share/ublue-os/image-info.json"

install -d -m0755 /usr/share/ublue-os

# Keep image-info compatible with Bluefin tooling while preserving Utah's
# published image name and flavor.
cat >"${IMAGE_INFO}" <<EOF
{
  "image-name": "${IMAGE_NAME}",
  "image-flavor": "${IMAGE_FLAVOR}",
  "image-vendor": "${IMAGE_VENDOR}",
  "image-ref": "ostree-image-signed:docker://ghcr.io/${IMAGE_VENDOR}/${IMAGE_NAME}",
  "image-tag": "${UBLUE_IMAGE_TAG}",
  "base-image-name": "${BASE_IMAGE_NAME}",
  "fedora-version": "${FEDORA_MAJOR_VERSION}"
}
EOF

# Replace Hummingbird/Fedora identity without assuming a particular ordering of
# os-release keys. All values are deliberately shell-quoted as os-release data.
set_os_release() {
    local key="$1" value="$2"
    if grep -q "^${key}=" /usr/lib/os-release; then
        sed -i "s|^${key}=.*|${key}=\"${value}\"|" /usr/lib/os-release
    else
        printf '%s="%s"\n' "${key}" "${value}" >> /usr/lib/os-release
    fi
}

set_os_release NAME "${IMAGE_PRETTY_NAME}"
set_os_release VARIANT_ID "${IMAGE_NAME}"
set_os_release PRETTY_NAME "${IMAGE_PRETTY_NAME} (Version: ${VERSION})"
set_os_release ID "${IMAGE_PRETTY_NAME,,}"
set_os_release ID_LIKE "${IMAGE_LIKE}"
set_os_release VERSION_ID "${FEDORA_MAJOR_VERSION}"
set_os_release CPE_NAME "cpe:/o:universal-blue:utah"
set_os_release HOME_URL "https://projectbluefin.io"
set_os_release DOCUMENTATION_URL "https://docs.projectbluefin.io"
set_os_release SUPPORT_URL "https://github.com/projectbluefin/utah/issues/"
set_os_release BUG_REPORT_URL "https://github.com/projectbluefin/utah/issues/"
set_os_release DEFAULT_HOSTNAME "${IMAGE_PRETTY_NAME,,}"
set_os_release VERSION_CODENAME "Utahraptor"
set_os_release VERSION "${VERSION} (${BASE_IMAGE_NAME^})"
set_os_release OSTREE_VERSION "${VERSION}"
set_os_release IMAGE_ID "${IMAGE_NAME}"
set_os_release IMAGE_VERSION "${VERSION}"
set_os_release BUILD_ID "${SHA_HEAD_SHORT}"

# Fedora's bootloader helper still keys its vendor directory off EFIDIR after
# the distribution ID changes.
if [ -f /usr/sbin/grub2-switch-to-blscfg ]; then
    sed -i 's|^EFIDIR=.*|EFIDIR="fedora"|' /usr/sbin/grub2-switch-to-blscfg
fi

# These files are intentionally placeholders. The common Bluefin stats timer
# refreshes them after first boot; keeping them present avoids a blank fastfetch
# and matches the files shipped by Bluefin.
printf '…\n' >/usr/share/ublue-os/fastfetch-user-count
printf '…\n' >/usr/share/ublue-os/bazaar-install-count

printf 'Utah branding configured for %s (%s)\n' "${IMAGE_NAME}" "${IMAGE_FLAVOR}"
