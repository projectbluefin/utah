#!/usr/bin/env bash
# iso/scripts/build-iso-tacklebox.sh — build a Utah live ISO via tacklebox.
#
# tacklebox (https://github.com/tuna-os/tacklebox) turns a bootable OCI image
# into a UEFI live ISO (systemd-boot + tbox-live, no anaconda). This is the
# fast path for Utah images and variants the project does not publish ISOs
# for: it builds straight from a local or GHCR image ref, and the same image
# refs work as iso.tunaos.org ?image= presets (projectbluefin is on the relay
# allowlist).
#
# Pipeline (two stages, one ISO):
#   1. Bake (invoking user's rootless podman): iso/live/Containerfile.tacklebox
#      pre-installs the live Flatpaks into localhost/utah-tacklebox-<flavor>.
#      The bake cannot run in tacklebox live_customize: flatpak's deploy
#      sandbox (bwrap) must configure loopback, which needs a user namespace,
#      and tacklebox runs customize containers rootful with only CAP_SYS_ADMIN
#      (fails with "bwrap: loopback: Failed RTM_NEWADDR"). Rootless podman
#      provides the userns -- the same context iso/live/Containerfile builds in.
#   2. Assemble (root): tacklebox builds the ISO from the baked image, with
#      iso/live/src/configure-live.sh as the live_customize script and the
#      published image ref embedded as an offline payload.
#
# This does NOT replace iso/scripts/build-iso.sh. Differences that matter:
#   - Secure Boot: tacklebox emits an unsigned systemd-boot chain, so these
#     ISOs boot only with Secure Boot disabled. The shim/GRUB/MOK path in
#     build-iso.sh remains the only Secure Boot live ISO.
#   - The dmsquash-live initramfs rebuild is skipped: tacklebox builds its own
#     initramfs with the tbox-live modules from the image's kernel.
#
# Usage (run from the repository root):
#   sudo bash iso/scripts/build-iso-tacklebox.sh <flavor> [stream] [repo] [tag] [debug]
#     flavor   a config/flavors.json flavor (main, nvidia, gaming, ...)
#     stream   image stream (default: testing)
#     repo     local | ghcr (default: local)
#     tag      image tag for ghcr pulls (default: <stream>);
#              sha256:... digest pins the exact payload (CI)
#     debug    1 enables the live sshd/password path (default: 0)
#
# Env:
#   REPO_ORGANIZATION        GHCR org (default: projectbluefin)
#   TACKLEBOX_BIN            host tacklebox binary (preferred: nested podman
#                            breaks container DNS on some hosts; extract with
#                            podman cp from ghcr.io/tuna-os/tacklebox:latest)
#   TACKLEBOX_IMAGE          tacklebox container (default: ghcr.io/tuna-os/tacklebox:latest)
#   TACKLEBOX_FROM_SOURCE=1  build tacklebox from git instead (needs go)
#   TACKLEBOX_SHA            pinned commit for from-source builds
#   TACKLEBOX_TIMEOUT_SECONDS  deadline for the tacklebox invocation (default: 4800)
#
# Outputs output/utah-<flavor>-tacklebox.iso.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "ERROR: tacklebox needs root for loop devices, sgdisk, mkfs and mounts." >&2
    echo "Run: sudo $0 $*" >&2
    exit 1
fi

if [[ ! -d "scripts" || ! -f "config/flavors.json" ]]; then
    echo "ERROR: run from the repository root (scripts/ or config/flavors.json not found)." >&2
    exit 1
fi

# Root-context podman must use root's store, never the invoking user's: a
# rootless store holds user-owned files that mksquashfs (real root, no user
# namespace) would record with the wrong uid, and probes would find the image
# in the wrong store. See tuna-os/tunaos scripts/build-iso-tacklebox.sh.
REAL_USER="${SUDO_USER:-}"
if [[ -n "$REAL_USER" && "$REAL_USER" != "root" ]]; then
    export HOME=/root
    unset XDG_DATA_HOME XDG_CONFIG_HOME
fi

# Tacklebox stages the rebuilt initramfs via os.MkdirTemp, which honors
# TMPDIR. Point it off the (often small) /tmp onto /var, which also holds
# the output dir and the container store.
export TMPDIR="${TMPDIR:-/var/tmp}"
mkdir -p "$TMPDIR"

FLAVOR="${1:?usage: $0 <flavor> [stream] [repo] [tag] [debug]}"
STREAM="${2:-testing}"
REPO="${3:-local}"
TAG="${4:-$STREAM}"
DEBUG="${5:-0}"
ORG="${REPO_ORGANIZATION:-projectbluefin}"

# Never carry a flavored image name literally: flavors.json is the single
# source, and `just check` fails on literals.
IMAGE_NAME="$(python3 scripts/flavors.py image "$FLAVOR")"
MEDIA_ID="utah-${FLAVOR}"
# ISO 9660 volume IDs cap at 32 characters.
if [[ ${#MEDIA_ID} -gt 32 ]]; then
    echo "ERROR: media id ${MEDIA_ID} exceeds the 32-character volume ID limit." >&2
    exit 1
fi
# Constructed from flavors.py output, never literal (the check gate greps
# iso/scripts/ for literals too).

if [[ "$REPO" == "local" ]]; then
    IMAGE_REF="localhost/${IMAGE_NAME}:${STREAM}"
    if ! podman image exists "$IMAGE_REF"; then
        # The image may live in the invoking user's rootless store (where
        # `just build-ghcr` leaves it). Import it into root's store so file
        # ownership in the squashfs is recorded correctly.
        if [[ -n "$REAL_USER" && "$REAL_USER" != "root" ]] && \
                sudo -u "$REAL_USER" podman image exists "$IMAGE_REF"; then
            echo "==> Importing ${IMAGE_REF} from ${REAL_USER}'s store into root's store..."
            sudo -u "$REAL_USER" podman save "$IMAGE_REF" | podman load
        else
            echo "ERROR: image $IMAGE_REF not found; build it first" >&2
            echo "  just build-ghcr utah $STREAM $FLAVOR" >&2
            exit 1
        fi
    fi
elif [[ "$REPO" == "ghcr" ]]; then
    # TAG may be a floating tag (testing) or an exact digest (sha256:...,
    # as resolved by CI) -- digests pin the payload precisely.
    if [[ "$TAG" == sha256:* ]]; then
        IMAGE_REF="ghcr.io/${ORG}/${IMAGE_NAME}@${TAG}"
    else
        IMAGE_REF="ghcr.io/${ORG}/${IMAGE_NAME}:${TAG}"
    fi
    echo "==> Pulling ${IMAGE_REF} into root's store..."
    podman pull "$IMAGE_REF"
else
    echo "ERROR: repo must be local or ghcr, got '$REPO'." >&2
    exit 1
fi

# ── Stage 1: bake the live Flatpaks (invoking user's podman) ────────────────
# Must run rootless: flatpak's bwrap sandbox needs a user namespace, which a
# rootful build container does not have. Direct root invocation has no user
# store to build in, so it is refused with guidance instead of failing late.
if [[ -z "$REAL_USER" || "$REAL_USER" == "root" ]]; then
    echo "ERROR: run under sudo from a login shell (need \$SUDO_USER for the rootless bake)." >&2
    exit 1
fi
BAKE_REF="localhost/utah-tacklebox-${FLAVOR}:${STREAM}"
echo "==> Baking live Flatpaks as ${REAL_USER}: ${BAKE_REF}"
sudo -u "$REAL_USER" podman build \
    --build-arg "SOURCE_IMAGE=${IMAGE_REF}" \
    --tag "$BAKE_REF" \
    --file iso/live/Containerfile.tacklebox iso/live/
echo "==> Importing ${BAKE_REF} into root's store..."
sudo -u "$REAL_USER" podman save "$BAKE_REF" | podman load

# The name embedded in the offline store stays the canonical published ref no
# matter where this ISO was built from, so installs resolve it identically.
if [[ "$REPO" == "local" ]]; then
    PAYLOAD_REF="ghcr.io/${ORG}/${IMAGE_NAME}:${STREAM}"
else
    # Tag or digest: whatever was pulled above is what installs resolve.
    PAYLOAD_REF="$IMAGE_REF"
fi

OUT_DIR="$(pwd)/output/.build-tacklebox/${FLAVOR}"
RECIPE_FILE="${OUT_DIR}/recipe.json"
ISO_OUT="${OUT_DIR}/${MEDIA_ID}.iso"
FINAL_ISO="$(pwd)/output/${MEDIA_ID}-tacklebox.iso"

# An aborted build leaves the offline store's bind mount behind; the next run
# then dies at cleanup after a full rebuild. Release it here instead.
stale_store="${OUT_DIR}/tbox-offline-store/overlay"
if mountpoint -q "$stale_store" 2>/dev/null; then
    echo "==> Releasing stale offline-store mount: ${stale_store}"
    umount -l "$stale_store" || true
fi

# ── Stage 2: assemble (root) ────────────────────────────────────────────────
# Private customization directory: configure-live.sh runs verbatim against the
# baked image. The Flatpaks it expects (fisherman lookup) are baked in stage 1.
CUSTOMIZE_DIR="${OUT_DIR}/live-customize"
rm -rf "$CUSTOMIZE_DIR"
mkdir -p "$CUSTOMIZE_DIR"
cp -a "iso/live/src/." "$CUSTOMIZE_DIR/"
cat >"${CUSTOMIZE_DIR}/customize-live.sh" <<EOF
#!/usr/bin/env bash
# tacklebox live_customize entrypoint: Utah's live configuration.
# The Flatpaks (installer bundle, Ghostty) are baked into the image by
# iso/live/Containerfile.tacklebox; only configuration happens here.
set -euo pipefail
cd "\$(dirname "\${BASH_SOURCE[0]}")"
export TARGET_IMAGE="${PAYLOAD_REF}"
export DEBUG="${DEBUG}"
bash ./configure-live.sh
EOF
chmod +x "${CUSTOMIZE_DIR}/customize-live.sh"

# enforcing=0: the live squashfs root is unlabeled, exactly as in
# iso/scripts/build-iso.sh. console=ttyS0 keeps the serial E2E readable.
# compression=release (zstd-15, 1 MiB blocks) for the live rootfs and the
# offline store: at tacklebox's fast default the ISO overshoots the 6 GiB
# budget (#128) that post-testing-e2e enforces.
cat >"$RECIPE_FILE" <<EOF
{
  "media_name": "${MEDIA_ID}",
  "size": "10G",
  "shared_store": {
    "format": "ext4",
    "compression": "release"
  },
  "kargs": ["enforcing=0", "console=ttyS0,115200n8"],
  "bootable_environments": [
    {
      "id": "${MEDIA_ID}",
      "image": "${BAKE_REF}",
      "desktop": "gnome",
      "live_customize": ["${CUSTOMIZE_DIR}/customize-live.sh"],
      "modes": ["live"]
    }
  ],
  "offline_payloads": [
    {
      "source": "${IMAGE_REF}",
      "ref": "${PAYLOAD_REF}"
    }
  ]
}
EOF
python3 -m json.tool "$RECIPE_FILE" >/dev/null

echo "==> Building ISO with tacklebox..."
echo "    image:   ${BAKE_REF} (live-baked)"
echo "    payload: ${PAYLOAD_REF} (embedded offline ref)"
echo "    recipe:  ${RECIPE_FILE}"
echo "    output:  ${ISO_OUT}"

TACKLEBOX_IMAGE="${TACKLEBOX_IMAGE:-ghcr.io/tuna-os/tacklebox:latest}"
TIMEOUT_SECONDS="${TACKLEBOX_TIMEOUT_SECONDS:-4800}"
[[ "$TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ ]] || {
    echo "ERROR: TACKLEBOX_TIMEOUT_SECONDS must be a positive integer." >&2
    exit 2
}

if [[ -n "${TACKLEBOX_BIN:-}" ]]; then
    [[ -x "$TACKLEBOX_BIN" ]] || { echo "ERROR: TACKLEBOX_BIN=$TACKLEBOX_BIN is not executable." >&2; exit 1; }
    echo "==> Using host tacklebox binary: ${TACKLEBOX_BIN}"
    TB=("$TACKLEBOX_BIN")
elif command -v tacklebox >/dev/null; then
    echo "==> Using tacklebox from PATH: $(command -v tacklebox)"
    TB=(tacklebox)
elif [[ "${TACKLEBOX_FROM_SOURCE:-0}" == "1" ]]; then
    command -v go >/dev/null || { echo "ERROR: TACKLEBOX_FROM_SOURCE=1 needs go on PATH." >&2; exit 1; }
    SHA="${TACKLEBOX_SHA:-}"
    [[ -n "$SHA" ]] || { echo "ERROR: set TACKLEBOX_SHA to pin the tacklebox source." >&2; exit 1; }
    CACHE="${TACKLEBOX_CACHE:-/var/cache/utah/tacklebox}"
    mkdir -p "$CACHE"
    (
        cd "$CACHE"
        [[ -d .git ]] || git clone --quiet https://github.com/tuna-os/tacklebox.git .
        git fetch --quiet origin
        git -c advice.detachedHead=false checkout --quiet "$SHA"
        go build -o tacklebox ./cmd/tacklebox
    )
    TB=("$CACHE/tacklebox")
else
    echo "==> Using tacklebox image: ${TACKLEBOX_IMAGE}"
    podman pull "$TACKLEBOX_IMAGE" >/dev/null
    # --privileged for loop devices; the out dir is mounted at the same path
    # so the absolute live_customize path in the recipe resolves inside too.
    # Prefer TACKLEBOX_BIN on hosts where nested podman breaks container DNS.
    TB=(podman run --rm --privileged
        --security-opt label=disable
        --log-driver=k8s-file
        -v /var/lib/containers:/var/lib/containers
        -v /dev:/dev
        -v "$(realpath "$OUT_DIR"):$(realpath "$OUT_DIR")"
        -v "$(realpath "$RECIPE_FILE"):$(realpath "$RECIPE_FILE"):ro"
        "$TACKLEBOX_IMAGE")
fi

if timeout --foreground --kill-after=120 "$TIMEOUT_SECONDS" \
        "${TB[@]}" build "$(realpath "$RECIPE_FILE")" \
        --iso "$(realpath "$ISO_OUT")" \
        --output-base "$(realpath "$OUT_DIR")" \
        --yes; then
    rc=0
else
    rc=$?
    if [[ "$rc" -eq 124 || "$rc" -eq 137 ]]; then
        echo "ERROR: tacklebox exceeded its ${TIMEOUT_SECONDS}s deadline." >&2
    fi
    exit "$rc"
fi

cp "$ISO_OUT" "$FINAL_ISO"
if [[ -n "$REAL_USER" ]]; then
    chown "${SUDO_UID:-$(id -u "$REAL_USER")}:${SUDO_GID:-$(id -g "$REAL_USER")}" "$FINAL_ISO" "$ISO_OUT" || true
fi

echo ""
echo "==> Done! ISO: ${FINAL_ISO} ($(du -h "$FINAL_ISO" | cut -f1))"
echo "    NOTE: unsigned systemd-boot chain — boots with Secure Boot DISABLED only."
echo "    For a Secure Boot ISO use: just iso $STREAM"
