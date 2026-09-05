#!/usr/bin/env bash
# The tag under which the kernel cache image is published.
#
# It must change whenever anything the cache image contains would change, and
# not otherwise.  Those inputs are the base image it is built from, the two
# scripts that do the building, and the repositories the toolchain comes from --
# a different compiler produces a different kernel -- so the tag is a hash of
# exactly those.
#
# Hashing the whole scripts, comments included, is deliberate: it can only ever
# rebuild something that did not need rebuilding, never reuse something stale.
# A cheaper key that hashed just the version pins would miss a change to how
# the kernel is configured or how the module is linked.
set -euo pipefail
cd "$(dirname "$0")/.."
{
  grep -m1 '^ARG BASE_IMAGE=' Containerfile.kernel
  cat scripts/install-ogc-kernel.sh scripts/install-nvidia.sh
  cat packages/hummingbird.repo packages/fedora-44.repo
} | sha256sum | cut -c1-16
