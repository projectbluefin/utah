#!/usr/bin/env bash
# Fetch one file from projectbluefin/artwork at the commit pinned in
# config/artwork.lock and verify it against the lock's sha256 before it is
# written to its destination.
#
#   fetch-artwork.sh <path in the artwork repository> <destination file>
#
# The lock holds the commit once and a sha256 for every file Utah takes, so a
# path that is not listed fails rather than being fetched unchecked, and moving
# the commit without moving the checksums fails the same way. raw.githubusercontent.com
# serves a commit-addressed path immutably, which a tag or branch would not.
#
# Used by the `artwork` stage of the Containerfile. UTAH_ARTWORK_LOCK overrides
# the lock path and UTAH_ARTWORK_BASE the URL prefix (tests point it at a
# file:// tree).
set -euo pipefail

if [ "$#" -ne 2 ]; then
    echo "usage: fetch-artwork.sh <artwork repo path> <destination file>" >&2
    exit 2
fi
path="$1"
dest="$2"

lock="${UTAH_ARTWORK_LOCK:-$(dirname "${BASH_SOURCE[0]}")/../config/artwork.lock}"
repo_url="${UTAH_ARTWORK_BASE:-https://raw.githubusercontent.com/projectbluefin/artwork}"

commit="$(awk '$1 == "commit" && NF == 2 { print $2 }' "${lock}")"
if ! [[ "${commit}" =~ ^[0-9a-f]{40}$ ]]; then
    echo "fetch-artwork: ${lock} has no single 40-hex 'commit' line" >&2
    exit 1
fi

sum="$(awk -v p="${path}" 'NF == 2 && $2 == p { print $1 }' "${lock}")"
if ! [[ "${sum}" =~ ^[0-9a-f]{64}$ ]]; then
    echo "fetch-artwork: ${path} has no sha256 entry in ${lock}" >&2
    exit 1
fi

tmp="$(mktemp)"
trap 'rm -f "${tmp}"' EXIT
curl --fail --silent --show-error --location --retry 5 --retry-all-errors --retry-delay 2 \
    --output "${tmp}" "${repo_url}/${commit}/${path}"
echo "${sum}  ${tmp}" | sha256sum --check --strict --status || {
    echo "fetch-artwork: ${path} at ${commit} does not match the pinned sha256 ${sum}" >&2
    exit 1
}
install -Dm0644 "${tmp}" "${dest}"
