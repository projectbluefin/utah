#!/usr/bin/env bash
# Make the signed shim payload available in bootupd's component layout, so
# bootc can create a generic disk image without depending on the build host ESP.
#
# Two base image layouts exist and both are correct.
#
#   Old (sha256:c5539f9e...): the payload is staged under bootupd update tree,
#   at /usr/lib/bootupd/updates/EFI/fedora/, and /usr/lib/efi/shim does not
#   exist. Utah has to do the copy.
#
#   New (sha256:db1007fd...): the base already ships
#   /usr/lib/efi/shim/16.1-5/EFI/fedora/{shimx64,shim,mmx64}.efi and
#   BOOTX64.CSV, and /usr/lib/bootupd/updates/ holds only BIOS.json and
#   EFI.json. The mirroring has already been done upstream.
#
# This used to be four predicates chained with && at the tail of a long
# Containerfile RUN, hardcoding the old layout. On the new base it failed with
# no output at all: the last line of a successful build was "Utah desktop
# contract passed", then exit 1, three attempts out of three, with nothing to
# say whether shim-x64 was missing, the tree was missing, or the copy failed.
#
# So: succeed when the payload is already in place, copy when it is staged, and
# fail loudly and specifically when it is neither. A build that fails must say
# what it could not find.
set -euo pipefail

BOOTUPD_EFI="${BOOTUPD_EFI:-/usr/lib/bootupd/updates/EFI/fedora}"
SHIM_ROOT="${SHIM_ROOT:-/usr/lib/efi/shim}"

# Look for an existing payload before asking rpm anything. The base that ships
# it already is the base whose shim-x64 query we least want to depend on, and
# the directory it uses is named for the shim version without a dist tag, which
# need not match what rpm reports. Find the file that matters instead.
existing="$(find "${SHIM_ROOT}" -mindepth 4 -maxdepth 4 \
    -path '*/EFI/fedora/shimx64.efi' -print -quit 2>/dev/null || true)"
if [[ -n "${existing}" ]]; then
    echo "Base image already provides the shim payload at ${existing%/shimx64.efi}; nothing to mirror"
    exit 0
fi

fail() {
    echo "ERROR: shim mirroring: $*" >&2
    exit 1
}

if ! shim_version="$(rpm -q --qf '%{VERSION}-%{RELEASE}' shim-x64 2>&1)"; then
    fail "shim-x64 is not installed and no payload is present under ${SHIM_ROOT}
  (rpm -q said: ${shim_version}). The signed EFI payload comes from that
  package, so bootc cannot build a generic disk image without it. If the base
  image dropped shim-x64, it has to be added to the package contract rather
  than skipped here."
fi

if [[ ! -d "${BOOTUPD_EFI}" ]]; then
    parent="$(dirname "${BOOTUPD_EFI}")"
    echo "shim-x64 ${shim_version} is installed, but nothing is staged at ${BOOTUPD_EFI}" >&2
    echo "and nothing is present under ${SHIM_ROOT} either." >&2
    if [[ -d "${parent}" ]]; then
        echo "  ${parent} contains: $(ls -A "${parent}" 2>/dev/null | tr '\n' ' ')" >&2
    else
        echo "  ${parent} does not exist either." >&2
    fi
    fail "no EFI payload to mirror at ${BOOTUPD_EFI}"
fi

target="${SHIM_ROOT}/${shim_version}/EFI/fedora"
install -d "${target}"
cp -a "${BOOTUPD_EFI}/." "${target}/"
echo "Mirrored shim-x64 ${shim_version} EFI payload into ${target}"
