#!/usr/bin/env bash
# Measure the live-rootfs squashfs size of a Utah testing image under different
# embedded-container-store and compression strategies, for #875.
#
# This mirrors the store-embed + squashfs assembly of iso/scripts/build-iso.sh
# so the numbers compare to a real ISO. It intentionally does NOT assemble the
# ISO or boot it (that stays in build-iso.sh and the review VM); it measures the
# squashfs root only, which is the dominant term in the ISO because the offline
# container store is embedded inside it.
#
# Usage:
#   measure-iso-store.sh NAME=IMAGE [NAME=IMAGE ...] [OUTPUT_JSON]
#     NAME=IMAGE   one flavor to measure, e.g.
#                  main=ghcr.io/projectbluefin/utah:testing
#                  nvidia-gaming=ghcr.io/projectbluefin/utah-nvidia-gaming:testing
#     OUTPUT_JSON  results file (default: evidence/iso-size.json)
#
# Strategy space, each row name|driver|zstd-level|block-bytes|dedupe:
#   - driver:     overlay (production) vs vfs (flattened store layout)
#   - zstd-level: 3 (production) up to 15 (review "release"); 7, 11 in between
#   - block:      128 KiB (production) vs 1 MiB
#   - dedupe:     none vs hardlink. squashfs already dedups identical content
#                 across the whole image, so the hardlink row is expected to
#                 read ~0 and confirms that point rather than shrinking further.
#
# Requires podman, skopeo, mksquashfs, and the source image pulled locally.
set -euo pipefail
# Keep set -e inside the command substitutions below (measure_strategy runs in
# "$(...)"): without inherit_errexit bash ignores set -e there, so a failed
# skopeo copy or mksquashfs would be silently turned into a fabricated row.
shopt -s inherit_errexit

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${ROOT}"

OUTPUT_JSON="${OUTPUT_JSON:-evidence/iso-size.json}"

# Strategy matrix. Edit this list to add or remove candidate strategies; the
# script runs every row for every flavor given on the command line.
STRATEGIES=(
  "baseline|overlay|3|131072|none"
  "vfs-store|vfs|3|131072|none"
  "zstd-7|overlay|7|131072|none"
  "zstd-11|overlay|11|131072|none"
  "zstd-15|overlay|15|131072|none"
  "block-1m|overlay|3|1048576|none"
  "hardlink-dedupe|overlay|3|131072|hardlink"
)

# ---- JSON emitter ----------------------------------------------------------
_JSON=""
_json_first=1
_json_start() { _JSON="["; _json_first=1; }
_json_add() { # name driver level block dedupe squashfs_bytes rootfs_bytes
  local sep=","
  [[ ${_json_first} -eq 1 ]] && sep=""
  _json_first=0
  _JSON+="
${sep}{\"flavor\": \"$1\", \"strategy\": \"$2\", \"driver\": \"$3\", \"comp_level\": $4, \"block_size\": $5, \"dedupe\": \"$6\", \"squashfs_bytes\": $7, \"rootfs_uncompressed_bytes\": $8}"
}
_json_end() { _JSON+="
]"; }

# ---- per-strategy assembly (mirrors build-iso.sh) --------------------------
# Assembles one squashfs root for one strategy and prints "squashfs_bytes
# rootfs_uncompressed_bytes". Reuses build-iso.sh's exact skopeo + storage.conf
# + podman-run + mksquashfs commands so the number is comparable to a real ISO.
# The whole assembly runs inside `podman unshare` so rootless Podman accepts
# `image mount`, exactly as build-iso.sh does.
measure_strategy() { # LIVE_IMAGE PAYLOAD_IMAGE driver level block dedupe
  local LIVE_IMAGE="$1" PAYLOAD_IMAGE="$2" driver="$3" level="$4" block="$5" dedupe="$6"
  local WORK SQUASHFS_ROOT PAYLOAD_EXPORT PAYLOAD_STORE STORAGE_CONF
  WORK="$(mktemp -d "${UTAH_ISO_WORKDIR:-/var/tmp}/utah-measure.XXXXXX")"
  SQUASHFS_ROOT="${WORK}/squashfs-root"
  PAYLOAD_EXPORT="${WORK}/utah-payload"
  PAYLOAD_STORE="${WORK}/payload-store"
  STORAGE_CONF="${WORK}/payload-storage.conf"
  # Bake WORK and LIVE_IMAGE into the trap string (double quotes) so the EXIT
  # trap still sees them when it fires inside the command-substitution
  # subshell: a bare "${WORK}" there is already unset, printing
  # "WORK: unbound variable" and skipping cleanup (leaking ~13 GiB trees).
  trap "rm -rf '${WORK}'; podman image unmount '${LIVE_IMAGE}' >/dev/null 2>&1 || true" EXIT

  # build-iso.sh runs its whole mount/copy/skopeo/podman-run/squashfs assembly
  # inside `podman unshare` so rootless Podman accepts `image mount`; mirror it
  # here. The "squashfs_bytes rootfs_bytes" line is the only output that
  # escapes back to this function's stdout.
  podman unshare bash -s -- \
    "${LIVE_IMAGE}" "${PAYLOAD_IMAGE}" "${driver}" "${level}" "${block}" \
    "${dedupe}" "${WORK}" "${SQUASHFS_ROOT}" "${PAYLOAD_EXPORT}" \
    "${PAYLOAD_STORE}" "${STORAGE_CONF}" <<'ASSEMBLY'
set -euo pipefail
LIVE_IMAGE="$1"
PAYLOAD_IMAGE="$2"
driver="$3"
level="$4"
block="$5"
dedupe="$6"
WORK="$7"
SQUASHFS_ROOT="$8"
PAYLOAD_EXPORT="${9}"
PAYLOAD_STORE="${10}"
STORAGE_CONF="${11}"
cleanup() {
    set +e
    podman image unmount "${LIVE_IMAGE}" >/dev/null 2>&1
    rm -rf "${WORK}"
}
trap cleanup EXIT

MOUNT="$(podman image mount "${LIVE_IMAGE}")"
mkdir -p "${SQUASHFS_ROOT}"
cp -a "${MOUNT}/." "${SQUASHFS_ROOT}/"

mkdir -p "${PAYLOAD_STORE}"
# driver = overlay keeps one directory per layer; driver = vfs keeps a full
# flattened copy per layer. The vendor drop-in in the image sets overlay, but
# this run overrides CONTAINERS_STORAGE_CONF, so the driver here is real.
printf '[storage]\ndriver = "%s"\nrunroot = "/tmp/cs-runroot"\ngraphroot = "/payload-store"\n' "${driver}" > "${STORAGE_CONF}"

payload_source="containers-storage:${PAYLOAD_IMAGE}"
copy_flags=(--remove-signatures)
if [[ "${PAYLOAD_IMAGE}" == *@sha256:* ]]; then
    payload_source="docker://${PAYLOAD_IMAGE}"
    copy_flags+=(--preserve-digests)
fi
skopeo copy "${copy_flags[@]}" "${payload_source}" "dir:${PAYLOAD_EXPORT}"
podman run --rm --privileged \
    -v "${PAYLOAD_EXPORT}:/payload:ro" \
    -v "${PAYLOAD_STORE}:/payload-store" \
    -v "${STORAGE_CONF}:/tmp/storage.conf:ro" \
    "${LIVE_IMAGE}" sh -c 'mkdir -p /tmp/cs-runroot /var/tmp && CONTAINERS_STORAGE_CONF=/tmp/storage.conf skopeo copy --preserve-digests dir:/payload "containers-storage:$1"' sh "${PAYLOAD_IMAGE}"
mkdir -p "${SQUASHFS_ROOT}/usr/lib/containers/storage"
cp -a "${PAYLOAD_STORE}/." "${SQUASHFS_ROOT}/usr/lib/containers/storage/"

# dedupe=hardlink: replace store files of identical content with a hardlink to
# the rootfs copy. squashfs already dedups content, so this should read ~0; it
# exists to confirm that, not to shrink further. Hash only files under 16 MiB
# to keep the scan cheap (huge blobs barely change % size). The map is seeded
# from the rootfs EXCLUDING the store we are about to dedupe, and the second
# pass walks the store that actually lands in the squashfs root -- so the
# hardlinks really change what mksquashfs compresses.
if [[ "${dedupe}" == "hardlink" ]]; then
    declare -A HASH=()
    while IFS= read -r -d '' f; do
        h="$(sha256sum "${f}" | cut -d' ' -f1)"
        HASH["${h}"]="${f}"
    done < <(find "${SQUASHFS_ROOT}" -path "${SQUASHFS_ROOT}/usr/lib/containers/storage" -prune -o -type f -size -16M -print0)
    while IFS= read -r -d '' f; do
        h="$(sha256sum "${f}" | cut -d' ' -f1)"
        if [[ -n "${HASH[${h}]:-}" ]]; then
            rm -f "${f}"
            ln "${HASH[${h}]}" "${f}" 2>/dev/null || true
        fi
    done < <(find "${SQUASHFS_ROOT}/usr/lib/containers/storage" -type f -size -16M -print0)
fi

rootfs_bytes="$(du -sb "${SQUASHFS_ROOT}" | cut -f1)"
SQUASHFS="${WORK}/squashfs.img"
mksquashfs "${SQUASHFS_ROOT}" "${SQUASHFS}" \
    -noappend -comp zstd -Xcompression-level "${level}" -b "${block}" -processors 4 \
    -wildcards -e 'proc/*' -e 'sys/*' -e 'dev/*' -e run -e tmp
squashfs_bytes="$(du -b "${SQUASHFS}" | cut -f1)"
echo "${squashfs_bytes} ${rootfs_bytes}"
ASSEMBLY
}

# ---- build one flavor's live image -----------------------------------------
build_live_image() { # NAME IMAGE -> localhost/utah-live:NAME
  local NAME="$1"
  local IMAGE="$2"
  local LIVE_IMAGE="localhost/utah-live:${NAME}"
  echo "Building live image for ${NAME} from ${IMAGE}"
  if ! podman image exists "${LIVE_IMAGE}"; then
    podman build --layers \
      --cap-add sys_admin --security-opt label=disable \
      --build-arg SOURCE_IMAGE="${IMAGE}" \
      --build-arg TARGET_IMAGE="${IMAGE}" \
      --build-arg DEBUG="0" \
      --tag "${LIVE_IMAGE}" \
      --file iso/live/Containerfile iso/live
  fi
  echo "${LIVE_IMAGE}"
}

# ---- main ------------------------------------------------------------------
[[ $# -ge 1 ]] || { echo "usage: $0 NAME=IMAGE [NAME=IMAGE ...] [OUTPUT_JSON]" >&2; exit 2; }

_flavors=()
_images=()
for arg in "$@"; do
  [[ "${arg}" == *=* ]] || { echo "expected NAME=IMAGE, got '${arg}'" >&2; exit 2; }
  _flavors+=("${arg%%=*}")
  _images+=("${arg#*=}")
done

_json_start
for i in "${!_flavors[@]}"; do
  NAME="${_flavors[$i]}"
  IMAGE="${_images[$i]}"
  LIVE_IMAGE="$(build_live_image "${NAME}" "${IMAGE}")"
  for row in "${STRATEGIES[@]}"; do
    IFS='|' read -r name driver level block dedupe <<< "${row}"
    read -r squashfs_bytes rootfs_bytes <<< "$(measure_strategy "${LIVE_IMAGE}" "${IMAGE}" "${driver}" "${level}" "${block}" "${dedupe}")"
    echo "  ${NAME}/${name}: squashfs ${squashfs_bytes} B (rootfs ${rootfs_bytes} B uncompressed)"
    _json_add "${NAME}" "${name}" "${driver}" "${level}" "${block}" "${dedupe}" "${squashfs_bytes}" "${rootfs_bytes}"
  done
done
_json_end

mkdir -p "$(dirname "${OUTPUT_JSON}")"
cat > "${OUTPUT_JSON}" <<EOF
{
  "issue": "projectbluefin/utah#875",
  "note": "Squashfs-root sizes per strategy. The baseline row (production settings) is the reference; strategy rows are filled by running 'just measure-iso'.",
  "full_iso_reference": {
    "note": "Full ISO sizes from CI for context; the harness measures the squashfs root only.",
    "main": { "bytes": 7288455168, "gib": 6.79, "source": "post-testing E2E run 40107791411, 2026-10-24" },
    "nvidia-gaming": { "bytes": null, "source": "pending measurement" }
  },
  "strategies": ${_JSON}
}
EOF
echo "Wrote ${OUTPUT_JSON}"
