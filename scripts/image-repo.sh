#!/usr/bin/bash
# image-repo.sh — Utah's image-name -> upstream GitHub repo resolver,
# installed as /usr/local/libexec/utah-image-repo.
#
# Routes `utah*` to projectbluefin/utah; forwards every other call to
# common's `/usr/libexec/ublue-image-repo`, which remains the authoritative
# resolver for non-Utah image names. Without this shim, `ujust report` on
# Utah falls through common's grammar and lands in projectbluefin/common
# (projectbluefin/utah#446).
#
# Usage:
#   utah-image-repo [--default REPO] [IMAGE_NAME] [IMAGE_TAG]
#
# Mirrors the option loop of /usr/libexec/ublue-image-repo (options end on
# `--` or the first non-option; everything after is forwarded verbatim), and
# honors the same IMAGE_NAME/IMAGE_TAG env fallback common uses, so the
# short-circuit triggers whether the caller supplies the name as a positional
# or via the environment (projectbluefin/utah#465).

set -euo pipefail

UBLUE_IMAGE_REPO="/usr/libexec/ublue-image-repo"

DEFAULT=""
DEFAULT_SET=0

# Mirror common's option loop exactly: options are only recognised before the
# first positional, and both `--` and the first non-option end option parsing.
# Everything that remains in "$@" is the positional tail (IMAGE_NAME,
# IMAGE_TAG, ...) and is forwarded verbatim, so an empty IMAGE_NAME stays in
# position instead of promoting IMAGE_TAG into the name slot.
while (($# > 0)); do
    case "$1" in
        --default)
            if (($# < 2)); then
                printf 'usage: utah-image-repo [--default REPO] [IMAGE_NAME] [IMAGE_TAG]\n' >&2
                exit 2
            fi
            DEFAULT="$2"
            DEFAULT_SET=1
            shift 2
            ;;
        --default=*)
            DEFAULT="${1#--default=}"
            DEFAULT_SET=1
            shift
            ;;
        --)
            shift
            break
            ;;
        *)
            break
            ;;
    esac
done

# Mirror common's IMAGE_NAME binding exactly: positional first, env fallback.
# Without this, a caller that supplies IMAGE_NAME only via the environment
# (no positional) would fall through to common, whose resolver routes
# `IMAGE_NAME=utah*` to its `*` arm and never returns projectbluefin/utah.
NAME="${1-${IMAGE_NAME-}}"

if [[ "$NAME" == utah* ]]; then
    printf '%s\n' "projectbluefin/utah"
    exit 0
fi

# Forward to common's authoritative resolver, preserving the caller's
# --default so an unrecognised name still falls back to that, not to
# common's hard-coded default. Absent positionals are omitted entirely
# rather than forwarded as empty strings: common reads `${1-${IMAGE_NAME-}}`,
# so a synthesised empty "$1" would suppress its IMAGE_NAME/IMAGE_TAG env
# fallback. Positionals the caller did pass — including genuinely empty
# ones — are forwarded unchanged.
FORWARD=()
if ((DEFAULT_SET)); then
    FORWARD+=(--default "$DEFAULT")
fi
FORWARD+=("$@")

exec "$UBLUE_IMAGE_REPO" ${FORWARD[@]+"${FORWARD[@]}"}
