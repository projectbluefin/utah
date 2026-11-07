#!/usr/bin/env bash
# Replace the base image's Fedora kernel with the same Fedora kernel rebuilt
# with Utah's patches -- a temporary measure until Fedora ships the fix.
#
# Hummingbird does not build a kernel: os-minimal takes Fedora 44's from
# fedora-44-updates. Every kernel from 7.2 on (7.3.x and 7.4-rc1 included)
# verifies a whole btrfs data folio with fs-verity when only part of it has
# been read, so intact files read back as corrupt: `fs-verity ... FILE
# CORRUPTED!` with the hash of a zero block, and now and then EIO in userspace.
# 7.2 is where btrfs turned on large data folios by default, which made the
# partial read the common case. A composefs root is nothing but fs-verity
# objects, so a composefs install on btrfs (bootc --composefs-backend, or
# bootc-migrate's conversion) reads its own system files through the bug. The
# upstream fix is linux-next dcf8811f63af, "btrfs: fix the fsverity callback
# where unexpected range is verified"; no released kernel carries it yet.
#
# So the cache image rebuilds Fedora's own source RPM for the base kernel with
# packages/kernel-patches/*.patch dropped into the spec's linux-kernel-test.patch
# hook, which exists for exactly this. Same version, same config, one fix. Drop
# the patch, and this script, once a Fedora kernel with the fix reaches the
# base image.
#
#   install-fedora-kernel.sh build OUTDIR      rebuild into OUTDIR (cache builder)
#   install-fedora-kernel.sh install DIR [--devel]
#                                              swap the installed kernel for DIR's
#   install-fedora-kernel.sh check             fail unless the patched kernel is
#                                              the one installed
#
# The release sorts above Fedora's: 200.1.utah1.fc44 > 200.fc44, the numeric
# ".1" outranking "fc". It also stays below any later Fedora build, so the next
# Fedora kernel replaces this one.
set -euo pipefail
shopt -s nullglob

FEDORA_KERNEL_VERSION="${UTAH_FEDORA_KERNEL_VERSION:-7.2.15}"
FEDORA_KERNEL_RELEASE="${UTAH_FEDORA_KERNEL_RELEASE:-200.fc44}"
# The Koji copy signed with the Fedora 44 release key, checked against the key
# committed in packages/ and against this digest. Version, release and digest
# move together, with BASE_IMAGE.
FEDORA_KERNEL_SRPM_SHA256="${UTAH_FEDORA_KERNEL_SRPM_SHA256:-579a2ad0a24950e30b79d6a6b8e80a1fe2fc5fbf1b4424ab120f3e371e4590d0}"
UTAH_BUILDID=".1.utah1"
FEDORA_KEY="/etc/pki/rpm-gpg/RPM-GPG-KEY-fedora-44-primary"
FEDORA_KEY_ID="6d9f90a6"
PATCH_DIR="${UTAH_KERNEL_PATCH_DIR:-/usr/share/utah/kernel-patches}"
ROOT="${UTAH_FEDORA_KERNEL_ROOT:-}"

arch="$(uname -m)"
dist="${FEDORA_KERNEL_RELEASE##*.}"
specrelease="${FEDORA_KERNEL_RELEASE%.*}"
patched_release="${specrelease}${UTAH_BUILDID}.${dist}"
patched_kver="${FEDORA_KERNEL_VERSION}-${patched_release}.${arch}"
# The packages a bootc base carries. kernel-devel is never installed in an
# image; it goes into the cache for the module builds that need a tree.
image_packages=(kernel kernel-core kernel-modules kernel-modules-core kernel-modules-extra)
DNF="$(command -v dnf5 || command -v dnf)"

usage() {
  echo "usage: $0 build OUTDIR | install DIR [--devel] | check" >&2
  exit 2
}

# The module trees that belong to the distribution kernel: every tree except
# the OGC kernel's, which install-ogc-kernel.sh records.
base_trees() {
  local ogc="" d
  [ -f "${ROOT}/usr/lib/utah/ogc-kernel-release" ] && ogc="$(cat "${ROOT}/usr/lib/utah/ogc-kernel-release")"
  for d in "${ROOT}"/usr/lib/modules/*/; do
    d="${d%/}"; d="${d##*/}"
    [ "$d" = "$ogc" ] || echo "$d"
  done
}

check() {
  local trees
  trees="$(base_trees)"
  if [ "$trees" != "$patched_kver" ]; then
    echo "The distribution kernel is not Utah's patched build." >&2
    echo "  expected exactly: ${patched_kver}" >&2
    echo "  found: ${trees:-nothing}" >&2
    echo "Build on the kernel cache image (just kernel-cache-ref; just build-kernel-cache" >&2
    echo "builds it locally) rather than the pristine Hummingbird base: its kernel reports" >&2
    echo "intact fs-verity data on btrfs as corrupt (see install-fedora-kernel.sh)." >&2
    exit 1
  fi
}

build() {
  local out="$1" topdir=/var/tmp/utah-fedora-kernel srpm url sig patches name rpm
  local nvr="${FEDORA_KERNEL_VERSION}-${FEDORA_KERNEL_RELEASE}"
  patches=("${PATCH_DIR}"/*.patch)
  if [ "${#patches[@]}" -eq 0 ]; then
    echo "No kernel patches in ${PATCH_DIR}; there is nothing to rebuild for" >&2
    exit 1
  fi
  "$DNF" -y install rpm-build 'dnf5-command(builddep)'
  srpm="kernel-${nvr}.src.rpm"
  url="https://kojipkgs.fedoraproject.org/packages/kernel/${FEDORA_KERNEL_VERSION}/${FEDORA_KERNEL_RELEASE}/data/signed/${FEDORA_KEY_ID}/src/${srpm}"
  echo "Taking ${srpm} from ${url}"
  curl --retry 3 --retry-all-errors -fsSLo "/tmp/${srpm}" "$url"
  echo "${FEDORA_KERNEL_SRPM_SHA256}  /tmp/${srpm}" | sha256sum --check --strict
  rpmkeys --import "$FEDORA_KEY"
  # rpmkeys exits 0 for an unsigned package ("digests OK"), so the word
  # "signatures" has to be in the verdict, not just a zero status.
  if ! sig="$(rpmkeys --checksig "/tmp/${srpm}")" || [[ "$sig" != *"signatures OK"* ]]; then
    echo "kernel source RPM signature check failed: ${sig:-no output}" >&2
    exit 1
  fi
  rm -rf "$topdir"
  rpm -i --define "_topdir ${topdir}" "/tmp/${srpm}"
  rm -f "/tmp/${srpm}"
  # The spec applies linux-kernel-test.patch last and fails %prep if it does
  # not apply, so a patch that no longer fits stops the build here.
  cat "${patches[@]}" >"${topdir}/SOURCES/linux-kernel-test.patch"
  "$DNF" -y builddep --define "_topdir ${topdir}" "${topdir}/SPECS/kernel.spec"
  # Only what an image installs: no debug variants, debuginfo, UKI, tools,
  # perf, selftests, headers or documentation. dist is pinned because the
  # builder's own %dist is Hummingbird's, not the Fedora one the release names.
  rpmbuild -bb --target "$arch" \
    --define "_topdir ${topdir}" \
    --define "dist .${dist}" \
    --define "buildid ${UTAH_BUILDID}" \
    --define "_smp_mflags -j$(nproc)" \
    --with baseonly --without debug --without debuginfo --without efiuki \
    --without selftests --without perf --without tools --without doc \
    --without headers --without cross_headers --without kabichk --without kabidwchk \
    "${topdir}/SPECS/kernel.spec"
  install -d "$out"
  for name in "${image_packages[@]}" kernel-devel; do
    rpm="${topdir}/RPMS/${arch}/${name}-${FEDORA_KERNEL_VERSION}-${patched_release}.${arch}.rpm"
    test -f "$rpm" || { echo "rpmbuild produced no ${rpm##*/}" >&2; ls -1 "${topdir}/RPMS/${arch}" >&2; exit 1; }
    install -m0644 "$rpm" "$out/"
  done
  printf '%s\n' "$patched_kver" >"$out/kernel-release"
  rm -rf "$topdir"
}

install_kernel() {
  local dir="$1" devel="${2:-}" name old rpms=()
  test "$(cat "$dir/kernel-release")" = "$patched_kver"
  old="$(base_trees)"
  # Upgrade exactly the kernel packages the image already has; rpm -U replaces
  # the older build of each name in one transaction.
  for name in "${image_packages[@]}"; do
    rpm -q "$name" >/dev/null 2>&1 || continue
    rpms+=("${dir}/${name}-${FEDORA_KERNEL_VERSION}-${patched_release}.${arch}.rpm")
  done
  [ "$devel" = --devel ] && rpms+=("${dir}/kernel-devel-${FEDORA_KERNEL_VERSION}-${patched_release}.${arch}.rpm")
  rpm -Uvh "${rpms[@]}"
  # What the old kernel's scriptlets generated (its initramfs) is not owned by
  # any package, so the tree outlives the erase. bootc wants one kernel.
  for name in $old; do
    [ "$name" = "$patched_kver" ] || rm -rf "/usr/lib/modules/${name}"
  done
  check
}

case "${1:-}" in
  build) [ $# -eq 2 ] || usage; build "$2" ;;
  install) [ $# -ge 2 ] || usage; install_kernel "$2" "${3:-}" ;;
  check) [ $# -eq 1 ] || usage; check ;;
  *) usage ;;
esac
