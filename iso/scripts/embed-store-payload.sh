#!/usr/bin/env bash
# Embed the install payload into the live root as an unpacked containers-storage
# overlay graphroot and write the receipt that binds fisherman to it.
#
# Fisherman refuses to install from live media (the
# /etc/bootc-installer/live-iso-mode marker) unless
# /etc/bootc-installer/media-input.json names a verified read-only payload
# (tuna-os/fisherman internal/mediainput, schema v1;
# internal/install/bound_media_linux.go). v1 offers two forms:
#
#   - ociLayoutPath: an OCI layout. Its layer blobs are tar files, so squashfs
#     stores them as a second copy of the image that shares no blocks with the
#     live root. PR #1131 run 43870670314 measured that: 9.4-11.3 GiB of
#     squashfs per flavor at zstd 15, against 5.2-5.8 GiB for the store form
#     in run 43815907502.
#   - containersStoragePath: an unpacked containers-storage graphroot. Its
#     files are byte-identical to the live root's, so mksquashfs ("duplicates
#     are removed") stores them once. This is the form used here.
#
# The store lives at /usr/lib/containers/storage in the squashfs. The live
# system bind-mounts it read-only at /var/lib/containers/storage, the only
# path v1 binds (iso/live/src/configure-live.sh).
#
# Usage: embed-store-payload.sh PAYLOAD_IMAGE PUBLISHED_IMAGE SQUASHFS_ROOT WORK LIVE_IMAGE
#   PAYLOAD_IMAGE    the image build-iso.sh was given (may be a localhost ref)
#   PUBLISHED_IMAGE  the publishable ref the installed system tracks; the
#                    receipt's `image` and `targetImgref`. Pinned by digest
#                    (name@sha256:...) in CI, in which case PAYLOAD_IMAGE must
#                    be the same ref and its registry blobs are copied.
#   SQUASHFS_ROOT    live root tree that becomes the squashfs
#   WORK             scratch directory; the caller removes it
#   LIVE_IMAGE       the live image: its skopeo writes the store, so the store
#                    is the one the live system's own c/storage reads
#
# Needs podman, skopeo and python3 on the build host. Run it inside
# `podman unshare` like the rest of the assembly, so the store belongs to the
# same uid as the root tree.
set -euo pipefail

PAYLOAD_IMAGE="${1:?payload image is required}"
PUBLISHED_IMAGE="${2:?published image is required}"
ROOT="${3:?squashfs root is required}"
WORK="${4:?scratch directory is required}"
LIVE_IMAGE="${5:?live image is required}"

STORE_RELATIVE="usr/lib/containers/storage"
STORE_DIR="${ROOT}/${STORE_RELATIVE}"
# The only store path fisherman v1 binds: the default graphroot. The live
# system mounts the squashfs store there read-only.
BOUND_STORE="/var/lib/containers/storage"
RECIPE="${ROOT}/etc/bootc-installer/recipe.json"
CATALOG="${ROOT}/etc/bootc-installer/images.json"
RECEIPT="${ROOT}/etc/bootc-installer/media-input.json"
PAYLOAD_EXPORT="${WORK}/utah-payload"
PAYLOAD_STORE="${WORK}/payload-store"
PAYLOAD_META="${WORK}/payload-meta"
STORAGE_CONF="${WORK}/payload-storage.conf"

[[ -f "${RECIPE}" && -f "${CATALOG}" ]] || {
    echo "ERROR: the live root has no installer recipe or catalog under ${ROOT}/etc/bootc-installer" >&2
    exit 1
}
# containers-common ships /usr/lib/containers/storage as an empty additional
# image store: overlay-images/ and overlay-layers/ with empty lock files. That
# skeleton is replaced by the payload store below; anything else there would be
# merged into it, so it fails the build.
if [[ -e "${STORE_DIR}" ]]; then
    if [[ ! -d "${STORE_DIR}" ]] || [[ -n "$(find "${STORE_DIR}" -mindepth 1 \
            \( \( ! -type d ! -name '*.lock' \) -o \( -name '*.lock' ! -empty \) \) -print -quit)" ]]; then
        echo "ERROR: ${STORE_DIR} already exists and holds more than containers-common's empty store" >&2
        exit 1
    fi
    rm -rf "${STORE_DIR}"
fi

echo "Embedding ${PUBLISHED_IMAGE} for offline installation"
# Containers-storage exports uncompressed layers; recompression or OCI archive
# conversion changes the manifest digest. For immutable CI inputs, export the
# original registry blobs directly and retain their manifest bytes via dir.
# Both copies keep the manifest digest, so the store carries the bytes the
# caller measured (`skopeo inspect --raw`, or the pinned digest), and the
# receipt below is checked against what the store reports.
payload_source="containers-storage:${PAYLOAD_IMAGE}"
copy_flags=(--remove-signatures)
if [[ "${PUBLISHED_IMAGE}" == *@sha256:* ]]; then
    [[ "${PAYLOAD_IMAGE}" == "${PUBLISHED_IMAGE}" ]] || {
        echo 'Digest-pinned live image and offline payload must match' >&2; exit 1;
    }
    payload_source="docker://${PUBLISHED_IMAGE}"
    copy_flags+=(--preserve-digests)
fi
skopeo copy "${copy_flags[@]}" "${payload_source}" "dir:${PAYLOAD_EXPORT}"

# overlay, because that is what the image already resolves to: Hummingbird
# ships /usr/share/containers/storage.conf.d/00-vendor.conf with
# driver = "overlay", and drop-ins are applied after /etc/containers/storage.conf,
# so a store of another driver would be invisible to the live system.
mkdir -p "${PAYLOAD_STORE}" "${PAYLOAD_META}"
printf '[storage]\ndriver = "overlay"\nrunroot = "/tmp/cs-runroot"\ngraphroot = "/payload-store"\n' >"${STORAGE_CONF}"
# The same skopeo that wrote the store reports the digest fisherman will read
# back with `skopeo inspect containers-storage:<image>`.
podman run --rm --privileged \
    -v "${PAYLOAD_EXPORT}:/payload:ro" \
    -v "${PAYLOAD_STORE}:/payload-store" \
    -v "${PAYLOAD_META}:/payload-meta" \
    -v "${STORAGE_CONF}:/tmp/storage.conf:ro" \
    "${LIVE_IMAGE}" sh -c 'set -e
mkdir -p /tmp/cs-runroot /var/tmp
export CONTAINERS_STORAGE_CONF=/tmp/storage.conf
skopeo copy --preserve-digests dir:/payload "containers-storage:$1"
skopeo inspect "containers-storage:$1" >/payload-meta/inspect.json' sh "${PUBLISHED_IMAGE}"
mkdir -p "${STORE_DIR}"
cp -a "${PAYLOAD_STORE}/." "${STORE_DIR}/"

python3 - "${PAYLOAD_EXPORT}" "${PAYLOAD_META}/inspect.json" "${BOUND_STORE}" "${PUBLISHED_IMAGE}" "${RECIPE}" "${CATALOG}" "${RECEIPT}" <<'PY'
import hashlib
import json
import re
import sys
from pathlib import Path

export, inspect, store_path, image, recipe, catalog, receipt = sys.argv[1:]
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


def fail(message):
    sys.exit(f"ERROR: {message}")


manifest_bytes = Path(export, "manifest.json").read_bytes()
manifest_digest = "sha256:" + hashlib.sha256(manifest_bytes).hexdigest()
manifest = json.loads(manifest_bytes)
if manifest.get("schemaVersion") != 2 or "layers" not in manifest:
    fail("the payload is not a single-platform image manifest")
config_digest = manifest["config"]["digest"]
if not DIGEST.fullmatch(config_digest):
    fail(f"bad config digest {config_digest!r} in the payload manifest")
config = json.loads(Path(export, config_digest[len("sha256:"):]).read_text())

# Fisherman resolves the image with `skopeo inspect containers-storage:<image>`
# and refuses an install whose digest differs from the receipt's. The store
# must report the exact manifest the payload carried, or the digest-preserving
# copy into it did not hold.
stored = json.loads(Path(inspect).read_text()).get("Digest")
if stored != manifest_digest:
    fail(f"the embedded store reports digest {stored}, but the payload manifest is {manifest_digest}")

# The installed system tracks `targetImgref`; the installer sends the recipe's
# imgref as the image, and fisherman refuses a request that names another
# image than the receipt's. configure-live.sh writes the published ref into
# both, so a mismatch means the live root and the receipt disagree.
recipe_data = json.loads(Path(recipe).read_text())
for key in ("imgref", "targetImgref"):
    if recipe_data.get(key) != image:
        fail(f"recipe.json {key} is {recipe_data.get(key)!r}, not {image!r}")
catalog_data = json.loads(Path(catalog).read_text())
refs = {catalog_data.get("default_image")} | {i.get("imgref") for i in catalog_data.get("images", [])}
if refs != {image}:
    fail(f"images.json names {sorted(map(str, refs))}, not only {image!r}")

Path(receipt).write_text(json.dumps({
    "schemaVersion": 1,
    "kind": "utah-live",
    "image": image,
    "platformManifestDigest": manifest_digest,
    "configDigest": config_digest,
    "containersStoragePath": store_path,
    "targetImgref": image,
    "platform": f"{config['os']}/{config['architecture']}",
}, indent=2) + "\n")
print(Path(receipt).read_text(), end="")
PY
rm -rf "${PAYLOAD_EXPORT}" "${PAYLOAD_STORE}" "${PAYLOAD_META}" "${STORAGE_CONF}"
