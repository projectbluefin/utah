#!/usr/bin/env bash
# Embed the install payload into the live root as a read-only OCI layout and
# write the receipt that binds fisherman to it.
#
# Fisherman refuses to install from live media (the
# /etc/bootc-installer/live-iso-mode marker) unless
# /etc/bootc-installer/media-input.json names a verified OCI layout on a
# read-only filesystem (tuna-os/fisherman internal/mediainput, schema v1;
# internal/install/bound_media_linux.go). The shape follows tuna-os/tromso
# scripts/embed-oci-payload.sh.
#
# Usage: embed-oci-payload.sh PAYLOAD_IMAGE PUBLISHED_IMAGE SQUASHFS_ROOT WORK
#   PAYLOAD_IMAGE    the image build-iso.sh was given (may be a localhost ref)
#   PUBLISHED_IMAGE  the publishable ref the installed system tracks; the
#                    receipt's `image` and `targetImgref`. Pinned by digest
#                    (name@sha256:...) in CI, in which case PAYLOAD_IMAGE must
#                    be the same ref and its registry blobs are copied.
#   SQUASHFS_ROOT    live root tree that becomes the squashfs
#   WORK             scratch directory; the caller removes it
#
# Needs skopeo and python3 on the build host. Run it inside `podman unshare`
# like the rest of the assembly, so the layout belongs to the same uid as the
# root tree.
set -euo pipefail

PAYLOAD_IMAGE="${1:?payload image is required}"
PUBLISHED_IMAGE="${2:?published image is required}"
ROOT="${3:?squashfs root is required}"
WORK="${4:?scratch directory is required}"

RELATIVE="usr/lib/utah/payload"
LAYOUT="${ROOT}/${RELATIVE}"
# dracut's dmsquash-live mounts the squashfs read-only at /run/rootfsbase,
# below the writable live overlay. Fisherman refuses a layout on a writable
# filesystem, and the root's /usr/lib is the overlay.
LIVE_LAYOUT="/run/rootfsbase/${RELATIVE}"
RECIPE="${ROOT}/etc/bootc-installer/recipe.json"
CATALOG="${ROOT}/etc/bootc-installer/images.json"
RECEIPT="${ROOT}/etc/bootc-installer/media-input.json"
PAYLOAD_EXPORT="${WORK}/utah-payload"

[[ -f "${RECIPE}" && -f "${CATALOG}" ]] || {
    echo "ERROR: the live root has no installer recipe or catalog under ${ROOT}/etc/bootc-installer" >&2
    exit 1
}
[[ ! -e "${LAYOUT}" ]] || { echo "ERROR: ${LAYOUT} already exists" >&2; exit 1; }

echo "Embedding ${PUBLISHED_IMAGE} for offline installation"
# Containers-storage exports uncompressed layers; recompression or OCI archive
# conversion changes the manifest digest. For immutable CI inputs, export the
# original registry blobs directly and retain their manifest bytes via dir.
# Both copies keep the manifest digest, so the layout carries the bytes the
# caller measured (`skopeo inspect --raw`, or the pinned digest). The
# preserving copy into an OCI layout fails for a Docker-schema manifest
# rather than rewrite it; that must stop the build.
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
mkdir -p "${LAYOUT}"
skopeo copy --preserve-digests "dir:${PAYLOAD_EXPORT}" "oci:${LAYOUT}:payload"
rm -rf "${PAYLOAD_EXPORT}"
# Fisherman reads as root, but a layout the live user cannot read is a trap
# for anyone debugging from a shell.
chmod -R a+rX "${LAYOUT}"

python3 - "${LAYOUT}" "${LIVE_LAYOUT}" "${PUBLISHED_IMAGE}" "${RECIPE}" "${CATALOG}" "${RECEIPT}" <<'PY'
import json
import re
import sys
from pathlib import Path

layout, live_layout, image, recipe, catalog, receipt = sys.argv[1:]
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


def fail(message):
    sys.exit(f"ERROR: {message}")


def blob(digest):
    if not DIGEST.fullmatch(digest):
        fail(f"bad digest {digest!r} in the payload layout")
    return Path(layout, "blobs", "sha256", digest[len("sha256:"):])


if json.loads(Path(layout, "oci-layout").read_text()).get("imageLayoutVersion") != "1.0.0":
    fail("the payload layout is not OCI image layout 1.0.0")
manifests = json.loads(Path(layout, "index.json").read_text())["manifests"]
if len(manifests) != 1:
    fail(f"the payload layout lists {len(manifests)} manifests, want exactly 1")
manifest_digest = manifests[0]["digest"]
manifest = json.loads(blob(manifest_digest).read_text())
config_digest = manifest["config"]["digest"]
config = json.loads(blob(config_digest).read_text())
for layer in manifest["layers"]:
    if not blob(layer["digest"]).is_file():
        fail(f"layer {layer['digest']} is missing from the payload layout")

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
    "ociLayoutPath": live_layout,
    "targetImgref": image,
    "platform": f"{config['os']}/{config['architecture']}",
}, indent=2) + "\n")
print(Path(receipt).read_text(), end="")
PY
