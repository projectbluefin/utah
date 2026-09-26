#!/usr/bin/env bash
# Build v4l2loopback -- the virtual-camera module OBS and friends write into --
# for the kernels this image boots (projectbluefin/utah#291).
#
# Bluefin ships it as kmod-v4l2loopback from UBlue's akmods bundle, and that
# bundle is as unusable here as it is for NVIDIA (see install-nvidia.sh): it is
# published per exact Fedora kernel NEVR and never for Hummingbird's. Neither
# Hummingbird nor the Utah package factory carries a kmod either. What remains
# is the upstream source, which is one C file and compiles in seconds.
#
#   install-v4l2loopback.sh base [stagedir]
#       The distribution kernel. Every flavor boots it. Run with a stagedir by
#       the `v4l2loopback` builder stage in the Containerfile, which is where
#       kernel-devel gets installed, so the image never carries it. Run again
#       without one in the image itself, where the module the builder staged
#       is already present: that run only registers it with depmod and checks
#       it is there.
#   install-v4l2loopback.sh ogc
#       The OGC kernel on the gaming flavors. Its build tree is preserved in
#       the image by install-ogc-kernel.sh, the same tree NVIDIA's module is
#       compiled against, so no kernel-devel is involved.
#
# The userspace half of Bluefin's v4l2loopback package is v4l2loopback-ctl,
# the only user-visible file it owns; it is built with the base module. The
# module is not loaded at boot, matching Bluefin: OBS loads it on demand
# (`pkexec modprobe v4l2loopback ...`) when its virtual camera is started.
#
# The module is unsigned, like every other module Utah compiles (the OGC kernel
# and NVIDIA's). With Secure Boot enabled, kernel lockdown refuses to load it;
# see docs/skills/local-testing.md.
set -euo pipefail

usage() {
  echo "usage: install-v4l2loopback.sh base [stagedir] | ogc" >&2
  exit 2
}

target="${1:-}"
# stagedir: where a builder stage collects its output. Empty means the image
# itself. UTAH_V4L2LOOPBACK_ROOT relocates the image root for the unit tests.
stage="${2:-}"
root="${UTAH_V4L2LOOPBACK_ROOT:-}"
dest="${stage:-$root}"
DNF="$(command -v dnf5 || command -v dnf)"

# Upstream's own release tag and the SHA-256 of GitHub's archive of it. Fedora
# 44 (and therefore Bluefin) ships 0.15.4 as well. Both move together.
V4L2LOOPBACK_VERSION="${UTAH_V4L2LOOPBACK_VERSION:-0.15.4}"
V4L2LOOPBACK_SHA256="${UTAH_V4L2LOOPBACK_SHA256:-21a17702648aa6a937b88a93bd71ef9f547815ead28719cafcfcc247396643dc}"

# kernel-devel is not in Hummingbird's repository at all, and Fedora's updates
# repository only carries the newest kernel, which a digest-pinned base stops
# matching within days (7.2.5 is pinned, updates had already moved to 7.2.7
# when this was written). Koji keeps every build, and keeps a copy signed with
# the release key beside it. Taking that copy and checking its signature
# against the Fedora 44 key committed in packages/ trusts the same key the
# fedora-44 repository does, and unlike a recorded hash it does not go stale
# when BASE_IMAGE moves. The key is per Fedora release: a base on fc45 needs
# the fc45 key and its ID here.
FEDORA_KEY="/etc/pki/rpm-gpg/RPM-GPG-KEY-fedora-44-primary"
FEDORA_KEY_ID="6d9f90a6"

ogc_release=""
if [ -f "${root}/usr/lib/utah/ogc-kernel-release" ]; then
  ogc_release="$(cat "${root}/usr/lib/utah/ogc-kernel-release")"
fi

case "$target" in
  base)
    # The newest module tree that is not the OGC kernel, as install-nvidia.sh
    # identifies it.
    release="$(for d in "${root}"/usr/lib/modules/*/; do
                 d="${d%/}"; d="${d##*/}"
                 [ "$d" = "$ogc_release" ] || echo "$d"
               done | sort -V | tail -n1)"
    ;;
  ogc)
    [ -z "$stage" ] || usage
    release="$ogc_release"
    ;;
  *) usage ;;
esac

if [ -z "$release" ] || [ ! -d "${root}/usr/lib/modules/${release}" ]; then
  echo "No ${target} kernel module tree to build v4l2loopback for" >&2
  ls -1 "${root}/usr/lib/modules" >&2 || true
  exit 1
fi

module="usr/lib/modules/${release}/extra/v4l2loopback/v4l2loopback.ko"
tree="${root}/usr/lib/modules/${release}/build"

provide_kernel_devel() {
  local arch nv ver rel rpmfile url sig
  arch="${release##*.}"; nv="${release%.*}"; ver="${nv%%-*}"; rel="${nv#*-}"
  rpmfile="kernel-devel-${ver}-${rel}.${arch}.rpm"
  url="https://kojipkgs.fedoraproject.org/packages/kernel/${ver}/${rel}/data/signed/${FEDORA_KEY_ID}/${arch}/${rpmfile}"
  echo "Taking kernel-devel for ${release} from ${url}"
  curl --retry 3 --retry-all-errors -fsSLo "/tmp/${rpmfile}" "$url"
  rpmkeys --import "$FEDORA_KEY"
  # rpmkeys exits 0 for an unsigned package ("digests OK"), so the word
  # "signatures" has to be in the verdict, not just a zero status.
  if ! sig="$(rpmkeys --checksig "/tmp/${rpmfile}")" || [[ "$sig" != *"signatures OK"* ]]; then
    echo "kernel-devel signature check failed: ${sig:-no output}" >&2
    exit 1
  fi
  "$DNF" -y install "/tmp/${rpmfile}"
  rm -f "/tmp/${rpmfile}"
}

added_toolchain=()
build() {
  local pkg src=/tmp/v4l2loopback-${V4L2LOOPBACK_VERSION}
  # Only the distribution kernel's tree can be supplied from Koji; a missing
  # OGC tree means install-ogc-kernel.sh went wrong, so stop before installing
  # or fetching anything.
  if [ ! -d "$tree" ] && [ "$target" != base ]; then
    echo "No OGC kernel build tree at $tree" >&2
    exit 1
  fi
  for pkg in gcc make; do
    rpm -q "$pkg" >/dev/null 2>&1 || added_toolchain+=("$pkg")
  done
  if [ "${#added_toolchain[@]}" -gt 0 ]; then
    "$DNF" -y install "${added_toolchain[@]}"
  fi
  [ -d "$tree" ] || provide_kernel_devel
  test -d "$tree"

  curl --retry 3 --retry-all-errors -fsSLo /tmp/v4l2loopback.tar.gz \
    "https://github.com/v4l2loopback/v4l2loopback/archive/refs/tags/v${V4L2LOOPBACK_VERSION}.tar.gz"
  echo "${V4L2LOOPBACK_SHA256}  /tmp/v4l2loopback.tar.gz" | sha256sum --check --strict
  tar -C /tmp -xzf /tmp/v4l2loopback.tar.gz

  # Kbuild directly: the source's Kbuild names obj-m, so unlike NVIDIA's tree
  # there is no wrapper Makefile computing the module list. The .ko is asserted
  # below regardless, because an empty obj-m would still exit 0.
  make -j"$(nproc)" -C "$tree" M="$src" modules
  install -Dm0644 "$src/v4l2loopback.ko" "${dest}/${module}"
  if [ "$target" = base ]; then
    make -C "$src/utils"
    install -Dm0755 "$src/utils/v4l2loopback-ctl" "${dest}/usr/bin/v4l2loopback-ctl"
  fi
  rm -rf "$src" /tmp/v4l2loopback.tar.gz
}

if [ -f "${dest}/${module}" ]; then
  echo "v4l2loopback for ${release} is already staged; not rebuilding"
elif [ "$target" = base ] && [ -z "$stage" ]; then
  # The image run only registers what the builder stage staged. Compiling here
  # would install kernel-devel into a shipped layer, which is what the builder
  # stage exists to prevent.
  echo "No staged v4l2loopback module for ${release} at ${dest}/${module};" >&2
  echo "the Containerfile's v4l2loopback stage should have provided it." >&2
  exit 1
else
  build
fi

# A staging run stops here: modules.dep belongs to the image, and a depmod
# against the stagedir would write a partial one for the image to inherit.
[ -z "$stage" ] || exit 0

if [ "${#added_toolchain[@]}" -gt 0 ]; then
  "$DNF" -y remove "${added_toolchain[@]}"
fi
depmod ${root:+-b "$root"} -a "$release"
for need in "${root}/${module}" "${root}/usr/bin/v4l2loopback-ctl"; do
  if [ ! -f "$need" ]; then
    echo "v4l2loopback is incomplete for ${release}: ${need} is missing" >&2
    exit 1
  fi
done
modinfo ${root:+-b "$root"} -k "$release" v4l2loopback >/dev/null
echo "v4l2loopback ${V4L2LOOPBACK_VERSION} installed for ${release}"
