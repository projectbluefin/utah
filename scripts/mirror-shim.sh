#!/usr/bin/env bash
# Mirror the signed shim payload into bootupd's component layout.
#
# Fedora's shim package stages its EFI payload under bootupd's update tree,
# while bootupd discovers image-provided EFI components under /usr/lib/efi.
# Mirroring the payload lets bootc create a generic disk image without
# depending on the build host's ESP.
#
# This was four predicates chained with && at the tail of a long Containerfile
# RUN. When the base image bumped to a kernel-7.2 Hummingbird, the step began
# failing with no output at all: the last line of a successful build was
# "Utah desktop contract passed", then exit 1. Nothing said whether shim-x64
# was missing, or the bootupd tree was, or the copy failed -- and each has a
# different answer. A build that fails must say what it could not find.
set -euo pipefail

BOOTUPD_EFI="${BOOTUPD_EFI:-/usr/lib/bootupd/updates/EFI/fedora}"
SHIM_ROOT="${SHIM_ROOT:-/usr/lib/efi/shim}"

fail() {
    echo "ERROR: shim mirroring: $*" >&2
    exit 1
}

if ! shim_version="$(rpm -q --qf '%{VERSION}-%{RELEASE}' shim-x64 2>&1)"; then
    fail "shim-x64 is not installed in this image (rpm -q said: ${shim_version}).
  The signed EFI payload comes from that package, so bootc cannot build a
  generic disk image without it. If the base image dropped shim-x64, it has to
  be added to the package contract rather than skipped here."
fi

if [[ ! -d "${BOOTUPD_EFI}" ]]; then
    parent="$(dirname "${BOOTUPD_EFI}")"
    echo "shim-x64 ${shim_version} is installed but staged nothing at ${BOOTUPD_EFI}." >&2
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
