#!/usr/bin/bash
# Apply Utah's OS identity to the Hummingbird base after all packages and
# system-files overlays are present. Utah consumes Bluefin's desktop assets and
# defaults, but remains a distinct, supportable Project Bluefin variant.

set -eoux pipefail

IMAGE_PRETTY_NAME="Utah"
IMAGE_LIKE="fedora"
IMAGE_NAME="${IMAGE_NAME:-utah}"
# Canonical OS identity is always "utah", regardless of which image repository
# name a flavor was published under (utah-nvidia, utah-gaming, ...). Bluefin
# tooling and the desktop contract read IMAGE_ID and image-name to decide
# whether they are on a Bluefin-derived image; the flavor lives in
# image-flavor, not in the identity.
IMAGE_ID="${IMAGE_ID:-utah}"
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
  "image-name": "${IMAGE_ID}",
  "image-flavor": "${IMAGE_FLAVOR}",
  "image-vendor": "${IMAGE_VENDOR}",
  "image-ref": "ostree-image-signed:docker://ghcr.io/${IMAGE_VENDOR}/${IMAGE_NAME}",
  "image-tag": "${UBLUE_IMAGE_TAG}",
  "base-image-name": "${BASE_IMAGE_NAME}",
  "fedora-version": "${FEDORA_MAJOR_VERSION}"
}
EOF

# The scanner-facing identity fields (ID, VERSION_ID, CPE_NAME) are
# intentionally left at whatever the Hummingbird base ships. CVE scanners
# (Trivy, ...) answer "is this based on Hummingbird?" and pick their
# vulnerability database from these three keys; branding them to "utah" makes
# the image look like a non-Fedora image with no CVE data to match against.
# The cosmetic/branding fields below are what show up in boot entries and are
# safe to set. os-release keys are rewritten without assuming an ordering.
set_os_release() {
    local key="$1" value="$2"
    if grep -q "^${key}=" /usr/lib/os-release; then
        sed -i "s|^${key}=.*|${key}=\"${value}\"|" /usr/lib/os-release
    else
        printf '%s="%s"\n' "${key}" "${value}" >> /usr/lib/os-release
    fi
}

set_os_release NAME "${IMAGE_PRETTY_NAME}"
set_os_release VARIANT_ID "${IMAGE_ID}"
set_os_release PRETTY_NAME "${IMAGE_PRETTY_NAME} (Version: ${VERSION})"
set_os_release ID_LIKE "${IMAGE_LIKE}"
set_os_release HOME_URL "https://projectbluefin.io"
set_os_release DOCUMENTATION_URL "https://docs.projectbluefin.io"
set_os_release SUPPORT_URL "https://github.com/projectbluefin/utah/issues/"
set_os_release BUG_REPORT_URL "https://github.com/projectbluefin/utah/issues/"
set_os_release DEFAULT_HOSTNAME "${IMAGE_PRETTY_NAME,,}"
set_os_release VERSION_CODENAME "Utahraptor"
set_os_release VERSION "${VERSION} (${BASE_IMAGE_NAME^})"
set_os_release OSTREE_VERSION "${VERSION}"
set_os_release IMAGE_ID "${IMAGE_ID}"
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

# Compile the system dconf databases now so the GDM greeter picks up the
# org.gnome.login-screen logo override (etc/dconf/db/gdm.d/01-bluefin-gdm-logo)
# on first boot. dconf-update.service does this in the installed system; doing
# it here too means a malformed keyfile fails the build rather than the
# post-install E2E that originally caught this (#378). This does not validate
# the logo path: dconf only compiles keyfiles and stores the value as an opaque
# string, so a missing PNG compiles fine. The image is guarded separately by
# the [branding].files entry for /usr/share/ublue-os/bluefin-logos/bluefin.png
# in contracts/bluefin-desktop.toml, checked by utah-verify-desktop-contract
# immediately after this script runs. The command is a no-op on hosts without
# dconf installed (e.g. CI without gnome-desktop), so guard with the binary
# rather than skip outright.
if [ -x /usr/bin/dconf ]; then
    /usr/bin/dconf update
fi

printf 'Utah branding configured for %s (flavor %s)\n' "${IMAGE_NAME}" "${IMAGE_FLAVOR}"
