#!/usr/bin/env bash
# Verify the published tuna-os/tacklebox builder image's SLSA provenance
# attestation before it is run with --privileged (utah#918). Digest-locking the
# pin stops an accidental tag move; this stops an unsigned image -- or one
# attested by any other workflow or repo -- from running as root with /dev
# mounted. The build script and post-testing-e2e both call it on the image they
# are about to pull, so the check and the privileged run use the same pin.
set -euo pipefail

# release-image.yml publishes the attestation keylessly; pin its Fulcio identity
# so an attestation from any other workflow or repository is rejected. It runs
# on both main pushes and v* tags, so accept either ref -- anchored, so no
# other branch or workflow can match.
readonly ATTEST_IDENTITY_REGEXP='^https://github\.com/tuna-os/tacklebox/\.github/workflows/release-image\.yml@refs/(heads/main|tags/v[^/]+)$'

# Verify the Renovate-managed pin in config/tacklebox-image -- the same file
# both privileged consumers pull from -- unless TACKLEBOX_IMAGE overrides it.
IMAGE="${TACKLEBOX_IMAGE:-$(< config/tacklebox-image)}"
[[ "$IMAGE" =~ ^[^[:space:]@]+@sha256:[a-f0-9]{64}$ ]] || {
    echo "ERROR: tacklebox image must be digest-pinned: ${IMAGE}" >&2
    exit 2
}

# cosign is in neither the base image nor the runner's default apt list, so
# install the version-pinned binary and confirm its SHA before trusting it to
# verify the image that is about to run as root.
command -v cosign >/dev/null 2>&1 || {
    echo "==> Installing cosign v3.1.4 to verify the tacklebox attestation..."
    case "$(uname -m)" in
        x86_64) asset=cosign-linux-amd64; sha=424e2c97d0b45fd7a6b608a87f090f35633f4c23ece5f092d48bba11aaf4bbfa ;;
        aarch64) asset=cosign-linux-arm64; sha=ade83b7aac62e131cacfe96d958948b2150cc9efec46a570a2649b8e27dedad2 ;;
        *) echo "ERROR: $(uname -m) is not supported; install cosign manually." >&2; exit 1 ;;
    esac
    # Download outside any bin path and install only once the checksum
    # passes, so a mismatch never leaves an unverified binary on PATH.
    tmp=$(mktemp)
    trap 'rm -f "$tmp"' EXIT
    curl -fsSL --retry 3 --retry-delay 5 -o "$tmp" \
        "https://github.com/sigstore/cosign/releases/download/v3.1.4/${asset}"
    echo "${sha}  ${tmp}" | sha256sum --check --status
    install -m 0755 "$tmp" /usr/local/bin/cosign
}

echo "==> Verifying SLSA provenance of ${IMAGE} (utah#918)"
cosign verify-attestation \
    --type=https://slsa.dev/provenance/v1 \
    --certificate-oidc-issuer="https://token.actions.githubusercontent.com" \
    --certificate-identity-regexp="${ATTEST_IDENTITY_REGEXP}" \
    "${IMAGE}"
