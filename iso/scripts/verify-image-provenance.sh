#!/usr/bin/env bash
# Verify the new Utah image's SLSA provenance attestation before it is pulled
# and used as privileged e2e media (utah#1018). Digest-locking the pin stops an
# accidental tag move; this stops an unsigned image -- or one attested by any
# other workflow or repo -- from running the privileged ISO build and install
# tests. post-testing-e2e calls it on the exact ref the luks and tacklebox jobs
# are about to pull, so the check and the privileged use share the same pin.
set -euo pipefail

# The image is signed and attested inside projectbluefin/actions'
# reusable-build.yml (build.yml calls it for build_main and build_kernel), so
# the Fulcio SAN is that reusable workflow, not utah's own build.yml. Pin the
# signer workflow and require utah/testing as the source repository and ref, so
# an attestation from any other workflow, repository, or branch is rejected.
readonly SIGNER_WORKFLOW='projectbluefin/actions/.github/workflows/reusable-build.yml'

# The ref is a digest from the exact build being validated (resolve-e2e-inputs),
# so no config file: the pin and the pull are the same string.
IMAGE="${1:-}"
[[ -n "$IMAGE" ]] || { echo "ERROR: usage: verify-image-provenance.sh <digest-pinned image-ref>" >&2; exit 2; }
[[ "$IMAGE" =~ ^[^[:space:]@]+@sha256:[a-f0-9]{64}$ ]] || {
    echo "ERROR: image must be digest-pinned: ${IMAGE}" >&2
    exit 2
}

# gh attestation verify is the purpose-built verifier for an attestation
# produced by actions/attest-build-provenance: it checks the source repository
# and the signer workflow separately, which is exactly the two things this gate
# must confirm. The verify step passes GH_TOKEN (gh is not pre-authenticated on
# the runner), and the attestation is served from the GitHub API rather than the
# registry, so no cosign binary or registry login is needed.
echo "==> Verifying SLSA provenance of ${IMAGE} (utah#1018)"
gh attestation verify "oci://${IMAGE}" \
    --repo projectbluefin/utah \
    --source-ref refs/heads/testing \
    --signer-workflow "${SIGNER_WORKFLOW}" \
    --predicate-type https://slsa.dev/provenance/v1
