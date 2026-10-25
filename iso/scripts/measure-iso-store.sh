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
#   [OUTPUT_JSON=path] measure-iso-store.sh NAME=IMAGE [NAME=IMAGE ...]
#     NAME=IMAGE   one flavor to measure, e.g.
#                  main=ghcr.io/projectbluefin/utah:testing
#                  nvidia-gaming=ghcr.io/projectbluefin/$(flavors.py image nvidia-gaming):testing
#     OUTPUT_JSON  environment variable naming the results file
#                  (default: evidence/iso-size.json)
#
# The results file is rewritten after every row, so an interrupted run keeps
# the rows it finished. A row whose assembly fails is recorded with null byte
# counts and an "error" field and the run continues; the script then exits 1.
# Only "strategies" is replaced: "issue", "note" and "full_iso_reference" are
# carried over from an existing results file.
#
# Strategy space, each row name|driver|zstd-level|block-bytes|dedupe:
#   - driver:     overlay (production) vs vfs (flattened store layout)
#   - zstd-level: 3 (production) up to 15 (review "release"); 7, 11 in between
#   - block:      128 KiB (production) vs 1 MiB
#   - dedupe:     none vs hardlink. squashfs already dedups identical content
#                 across the whole image, so the hardlink row is expected to
#                 read ~0 and confirms that point rather than shrinking further.
#
# Requires podman, skopeo, mksquashfs, python3, curl (installer artwork
# fetch), and the source image pulled locally.
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
_json_add() { # flavor name driver level block dedupe squashfs_bytes rootfs_bytes [error]
  local sep=","
  [[ ${_json_first} -eq 1 ]] && sep=""
  _json_first=0
  local error=""
  [[ -n "${9:-}" ]] && error=", \"error\": \"$9\""
  _JSON+="
${sep}{\"flavor\": \"$1\", \"strategy\": \"$2\", \"driver\": \"$3\", \"comp_level\": $4, \"block_size\": $5, \"dedupe\": \"$6\", \"squashfs_bytes\": $7, \"rootfs_uncompressed_bytes\": $8${error}}"
}

# ---- per-strategy assembly (mirrors build-iso.sh) --------------------------
# Assembles one squashfs root for one strategy and prints "squashfs_bytes
# rootfs_uncompressed_bytes". Reuses build-iso.sh's exact skopeo + storage.conf
# + podman-run + mksquashfs commands so the number is comparable to a real ISO.
# The whole assembly runs inside `podman unshare` so rootless Podman accepts
# `image mount`, exactly as build-iso.sh does.
measure_strategy() { # LIVE_IMAGE PAYLOAD_IMAGE driver level block dedupe
  local LIVE_IMAGE="$1" PAYLOAD_IMAGE="$2" driver="$3" level="$4" block="$5" dedupe="$6"
  local WORK SQUASHFS_ROOT PAYLOAD_EXPORT PAYLOAD_STORE STORAGE_CONF
  # Explicit return: the caller runs this in an `if`, where set -e is off.
  WORK="$(mktemp -d "${UTAH_ISO_WORKDIR:-/var/tmp}/utah-measure.XXXXXX")" || return 1
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
  # escapes back to this function's stdout (tool output goes to stderr).
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
# skopeo, the in-container skopeo and mksquashfs print progress on stdout; the
# caller parses this shell's stdout as the byte pair. Keep the real stdout on
# fd 3 for that one line and send everything else to stderr.
exec 3>&1 1>&2
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
# flattened copy per layer. build-iso.sh notes a vendor drop-in
# (/usr/share/containers/storage.conf.d/00-vendor.conf) that forces overlay;
# STORAGE_DRIVER overrides config files and drop-ins, so it is set for the
# in-container skopeo too. The store layout is still checked after the copy
# instead of being assumed.
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
    "${LIVE_IMAGE}" sh -c 'mkdir -p /tmp/cs-runroot /var/tmp && CONTAINERS_STORAGE_CONF=/tmp/storage.conf STORAGE_DRIVER="$2" skopeo copy --preserve-digests dir:/payload "containers-storage:$1"' sh "${PAYLOAD_IMAGE}" "${driver}"
if [[ ! -d "${PAYLOAD_STORE}/${driver}" ]]; then
    echo "payload store has no ${driver}/ directory; the requested driver was not applied" >&2
    exit 1
fi
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
            ln -f "${HASH[${h}]}" "${f}"
        fi
    done < <(find "${SQUASHFS_ROOT}/usr/lib/containers/storage" -type f -size -16M -print0)
fi

rootfs_bytes="$(du -sb "${SQUASHFS_ROOT}" | cut -f1)"
SQUASHFS="${WORK}/squashfs.img"
mksquashfs "${SQUASHFS_ROOT}" "${SQUASHFS}" \
    -noappend -comp zstd -Xcompression-level "${level}" -b "${block}" -processors 4 \
    -wildcards -e 'proc/*' -e 'sys/*' -e 'dev/*' -e run -e tmp
squashfs_bytes="$(du -b "${SQUASHFS}" | cut -f1)"
echo "${squashfs_bytes} ${rootfs_bytes}" >&3
ASSEMBLY
}

# ---- build one flavor's live image -----------------------------------------
build_live_image() { # NAME IMAGE -> localhost/utah-live:NAME
  local NAME="$1"
  local IMAGE="$2"
  local LIVE_IMAGE="localhost/utah-live:${NAME}"
  # Progress and build output go to stderr: stdout is captured by the caller
  # and must carry only the image reference.
  # Rebuild every run, like build-iso.sh: the payload is re-read from
  # containers-storage:IMAGE, so a cached live image could pair an old rootfs
  # with a newer store after IMAGE is re-pulled. --layers keeps a rebuild
  # against an unchanged IMAGE cheap.
  echo "Building live image for ${NAME} from ${IMAGE}" >&2
  podman build --layers \
    --cap-add sys_admin --security-opt label=disable \
    --build-arg SOURCE_IMAGE="${IMAGE}" \
    --build-arg TARGET_IMAGE="${IMAGE}" \
    --build-arg DEBUG="0" \
    --tag "${LIVE_IMAGE}" \
    --file iso/live/Containerfile iso/live >&2
  echo "${LIVE_IMAGE}"
}

# ---- results file ----------------------------------------------------------
# Replace only "strategies"; keep "issue", "note" and "full_iso_reference" from
# an existing file so hand-filled reference sizes survive a re-run.
write_output() {
  mkdir -p "$(dirname "${OUTPUT_JSON}")"
  printf '%s\n]' "${_JSON}" | python3 -c '
import json, os, sys
path = sys.argv[1]
data = {
    "issue": "projectbluefin/utah#875",
    "note": "Squashfs-root sizes per strategy. The baseline row (production settings) is the reference; strategy rows are filled by running \u0027just measure-iso\u0027.",
    "full_iso_reference": {
        "note": "Full ISO sizes from CI for context; the harness measures the squashfs root only.",
        "main": {"bytes": 7288455168, "gib": 6.79, "source": "post-testing E2E run 40107791411, 2026-10-24"},
        "nvidia-gaming": {"bytes": None, "source": "pending measurement"},
    },
}
if os.path.exists(path):
    with open(path) as f:
        existing = json.load(f)
    for key in ("issue", "note", "full_iso_reference"):
        if key in existing:
            data[key] = existing[key]
data["strategies"] = json.load(sys.stdin)
tmp = path + ".tmp"
with open(tmp, "w") as f:
    json.dump(data, f, indent=2)
    f.write("\n")
os.replace(tmp, path)
' "${OUTPUT_JSON}"
}

# ---- main ------------------------------------------------------------------
[[ $# -ge 1 ]] || { echo "usage: [OUTPUT_JSON=path] $0 NAME=IMAGE [NAME=IMAGE ...]" >&2; exit 2; }

_flavors=()
_images=()
for arg in "$@"; do
  [[ "${arg}" == *=* ]] || { echo "expected NAME=IMAGE, got '${arg}'" >&2; exit 2; }
  _flavors+=("${arg%%=*}")
  _images+=("${arg#*=}")
done

# The live build context is iso/live/, which cannot reach repo-root scripts/.
# Stage the same two files build-iso.sh stages: the Containerfile COPYs the
# Flatpak parser, and configure-live.sh exits without the installer mascot
# (pinned in config/artwork.lock, not committed). The trap removes both.
cleanup_staged() {
  rm -f "${ROOT}/iso/live/src/utah-verify-desktop-contract.py" "${ROOT}/iso/live/src/images/utahraptor.png"
  rmdir "${ROOT}/iso/live/src/images" 2>/dev/null || true
}
trap cleanup_staged EXIT
cp scripts/verify-desktop-contract.py iso/live/src/utah-verify-desktop-contract.py
scripts/fetch-artwork.sh \
  assets/raster/dinos/jacob-schnurr/utah/utah-look/black-outline/utah-look-black-outline.png \
  iso/live/src/images/utahraptor.png >&2

_json_start
failed=0
for i in "${!_flavors[@]}"; do
  NAME="${_flavors[$i]}"
  IMAGE="${_images[$i]}"
  LIVE_IMAGE="$(build_live_image "${NAME}" "${IMAGE}")"
  for row in "${STRATEGIES[@]}"; do
    IFS='|' read -r name driver level block dedupe <<< "${row}"
    # One failed row must not discard the others: record it with null byte
    # counts and move on. A here-string around "$(...)" would hide the status.
    error=""
    if result="$(measure_strategy "${LIVE_IMAGE}" "${IMAGE}" "${driver}" "${level}" "${block}" "${dedupe}")"; then
      read -r squashfs_bytes rootfs_bytes <<< "${result}" || true
      if [[ ! "${squashfs_bytes:-}" =~ ^[0-9]+$ || ! "${rootfs_bytes:-}" =~ ^[0-9]+$ ]]; then
        echo "${NAME}/${name}: expected two byte counts, got '${result}'" >&2
        error="assembly did not report two byte counts"
      fi
    else
      error="assembly failed"
    fi
    if [[ -n "${error}" ]]; then
      echo "  ${NAME}/${name}: FAILED (${error})"
      failed=$((failed + 1))
      _json_add "${NAME}" "${name}" "${driver}" "${level}" "${block}" "${dedupe}" null null "${error}"
    else
      echo "  ${NAME}/${name}: squashfs ${squashfs_bytes} B (rootfs ${rootfs_bytes} B uncompressed)"
      _json_add "${NAME}" "${name}" "${driver}" "${level}" "${block}" "${dedupe}" "${squashfs_bytes}" "${rootfs_bytes}"
    fi
    write_output
  done
done

echo "Wrote ${OUTPUT_JSON}"
if [[ ${failed} -gt 0 ]]; then
  echo "${failed} strategy row(s) failed; see the \"error\" fields in ${OUTPUT_JSON}" >&2
  exit 1
fi
