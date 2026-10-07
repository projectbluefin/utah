#!/usr/bin/bash
# Print the `podman build` arguments that fix image-created metadata to
# SOURCE_DATE_EPOCH, one per line, for `mapfile -t`. Shared by build-ghcr,
# build-local and check-reproducible-build.sh so the guard cannot drift.
#
#   mapfile -t epoch_args < <(scripts/podman-epoch-args.sh "${source_date_epoch}")
#
# --source-date-epoch needs Podman >= 5.5. Older engines get no arguments and
# a warning: the image works, but its config is not reproducible.
# A second argument of rewrite also normalizes generated tar metadata.
# Callers must enable the in-image RPM epoch guard and use the build-input
# source epoch, so rewriting never clamps an unchanged RPM payload.

set -euo pipefail

epoch="${1:?usage: podman-epoch-args.sh SOURCE_DATE_EPOCH}"
PODMAN="${PODMAN:-podman}"

version="$("${PODMAN}" --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+' | head -1 || true)"
if [[ -n "${version}" ]] && [[ "$(printf '5.5\n%s\n' "${version}" | sort -V | head -1)" == "5.5" ]]; then
    printf '%s\n' --source-date-epoch "${epoch}"
    if [[ "${2:-}" == rewrite ]]; then
        printf '%s\n' --rewrite-timestamp
    fi
else
    echo "warning: podman ${version:-missing} < 5.5; building without reproducible timestamps" >&2
fi
