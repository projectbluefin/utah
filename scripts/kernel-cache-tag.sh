#!/usr/bin/env bash
# The tag under which the kernel cache image is published.
#
# It must change whenever anything the cache image contains would change, and
# not otherwise.  Those inputs are the recipe that builds it, the two scripts
# that do the building, and the repositories the toolchain comes from -- a
# different compiler produces a different kernel -- so the tag is a hash of
# exactly those.
#
# Containerfile.kernel is hashed whole, not just its ARG BASE_IMAGE= line: its
# body is the recipe.  The single RUN decides what lands in /cache-out and the
# final stage decides what is copied out of the builder, so an edit there
# changes the image's contents while leaving the base pin untouched.  Since CI
# builds the cache image only when its tag is not already published, a key that
# missed those edits would silently hand the three cached flavors an image
# built from the previous recipe.
#
# Hashing the whole files, comments included, is deliberate: it can only ever
# rebuild something that did not need rebuilding, never reuse something stale.
# A cheaper key that hashed just the version pins would miss a change to how
# the kernel is configured or how the module is linked.
set -euo pipefail
cd "$(dirname "$0")/.."
{
  cat Containerfile.kernel
  cat scripts/install-ogc-kernel.sh scripts/install-nvidia.sh
  cat packages/hummingbird.repo packages/fedora-44.repo
  # The builder imports this key to verify Hummingbird's RPMs, so a rotated key
  # is a different build root (tests/test_kernel_cache_key.py enforces it).
  cat packages/RPM-GPG-KEY-redhat-release-2
} | sha256sum | cut -c1-16
