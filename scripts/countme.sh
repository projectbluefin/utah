#!/usr/bin/bash
# Weekly countme analytics client for Utah.
#
# Sends one anonymous "I still exist" ping per week to the Project Bluefin
# analytics endpoint so the dashboard can track active systems. Installation
# age is bucketed (see ADR 0006) so the dashboard shows an age distribution
# rather than a single raw count.
#
# The ping is a plain GET; a non-zero exit or network failure is not an error
# worth paging anyone over, so every external step is best-effort.
#
# Opt out by creating /etc/projectbluefin/countme/disabled.
#
# Testability: the cohort bucket, label derivation, and URL are all pure and
# reproducible when the COUNTME_* environment variables are set, and
# COUNTME_DRY_RUN=1 prints the URL instead of sending it. See tests/test_countme.py.

set -euo pipefail

ENDPOINT="https://countme.projectbluefin.io/metalink"
EPOCH_FILE="${COUNTME_EPOCH_FILE:-/var/lib/utah-countme/epoch}"
# Runtime opt-out sentinel. COUNTME_DISABLE_FILE overrides it for tests only;
# the shipped default is fixed by the issue and must not move.
DISABLE_FILE="${COUNTME_DISABLE_FILE:-/etc/projectbluefin/countme/disabled}"

# Cohort boundaries in whole days, matching ADR 0006:
#   1: first week      <  7 days
#   2: 2-4 weeks       < 28 days
#   3: 5-24 weeks      < 168 days
#   4: >24 weeks       (everything older)
BUCKET_1_DAYS=7
BUCKET_2_DAYS=28
BUCKET_3_DAYS=168

# Derive the install-age cohort from an age expressed in whole days.
# Pure: no I/O, so tests can drive it directly.
bucket_for_age_days() {
    local age_days="$1"
    if [ "${age_days}" -lt "${BUCKET_1_DAYS}" ]; then
        printf '%s' 1
    elif [ "${age_days}" -lt "${BUCKET_2_DAYS}" ]; then
        printf '%s' 2
    elif [ "${age_days}" -lt "${BUCKET_3_DAYS}" ]; then
        printf '%s' 3
    else
        printf '%s' 4
    fi
}

# Read one key from an os-release file, stripping a single layer of surrounding
# quotes. Honors a COUNTME_* override first, then the ambient environment, then
# falls back to the supplied default.
os_release_value() {
    local file="$1" key="$2" override="$3" env_name="$4" default="$5"
    local value=""
    if [ -n "${!override:-}" ]; then
        value="${!override}"
    elif [ -r "${file}" ]; then
        local line k v
        while IFS='=' read -r k v; do
            if [ "${k}" = "${key}" ]; then
                v="${v%\"}"; v="${v#\"}"
                v="${v%\'}"; v="${v#\'}"
                value="${v}"
                break
            fi
        done < "${file}"
    fi
    if [ -z "${value}" ] && [ -n "${!env_name:-}" ]; then
        value="${!env_name}"
    fi
    printf '%s' "${value:-${default}}"
}

# Build the analytics URL from the derived labels and the cohort bucket.
# Pure: tests assert on its stdout in dry-run mode.
build_url() {
    local repo="$1" tag="$2" flavor="$3" arch="$4" bucket="$5"
    printf '%s?repo=%s&tag=%s&flavor=%s&arch=%s&countme=%s' \
        "${ENDPOINT}" "${repo}" "${tag}" "${flavor}" "${arch}" "${bucket}"
}

# Record the install epoch on first run so subsequent pings measure true age.
# Best-effort: if the directory is not writable we print the epoch back so the
# caller still reports the first-week bucket, which is harmless.
ensure_epoch() {
    local now="$1"
    if [ -r "${EPOCH_FILE}" ]; then
        return 0
    fi
    if mkdir -p "$(dirname "${EPOCH_FILE}")" 2>/dev/null && \
       printf '%s\n' "${now}" > "${EPOCH_FILE}" 2>/dev/null; then
        return 0
    fi
    printf '%s\n' "${now}"
}

main() {
    # Opt-out is a single sentinel file; creating it silences all future pings.
    if [ -e "${DISABLE_FILE}" ]; then
        printf 'countme: opt-out file present (%s), skipping\n' "${DISABLE_FILE}" >&2
        return 0
    fi

    local now
    now="$(date +%s)"

    local epoch
    if [ -n "${COUNTME_EPOCH:-}" ]; then
        epoch="${COUNTME_EPOCH}"
    else
        ensure_epoch "${now}"
        epoch="$(cat "${EPOCH_FILE}")"
    fi

    # A clock going backwards must not yield a negative age.
    local age_seconds
    age_seconds=$(( now - ${epoch:-now} ))
    if [ "${age_seconds}" -lt 0 ]; then
        age_seconds=0
    fi
    local age_days=$(( age_seconds / 86400 ))

    local bucket
    bucket="$(bucket_for_age_days "${age_days}")"

    local os_release_file="/etc/os-release"
    if [ -n "${COUNTME_OS_RELEASE:-}" ]; then
        os_release_file="${COUNTME_OS_RELEASE}"
    fi

    local repo tag flavor arch
    repo="$(os_release_value "${os_release_file}" "IMAGE_ID" COUNTME_REPO IMAGE_ID utah)"
    tag="$(os_release_value "${os_release_file}" "IMAGE_VERSION" COUNTME_TAG IMAGE_VERSION unknown)"
    flavor="$(os_release_value "${os_release_file}" "IMAGE_FLAVOR" COUNTME_FLAVOR IMAGE_FLAVOR main)"
    arch="$(os_release_value "${os_release_file}" "ARCH" COUNTME_ARCH ARCH "$(uname -m)")"

    local url
    url="$(build_url "${repo}" "${tag}" "${flavor}" "${arch}" "${bucket}")"

    if [ "${COUNTME_DRY_RUN:-0}" = "1" ]; then
        printf '%s\n' "${url}"
        return 0
    fi

    # Best-effort ping: short timeout, a couple of retries, result ignored.
    if command -v curl >/dev/null 2>&1; then
        curl -fsS --retry 2 --retry-delay 2 --max-time 15 \
            -o /dev/null -- "${url}" >/dev/null 2>&1 || true
    else
        # Fall back to /dev/tcp when curl is absent from the runtime image.
        { exec 3<>/dev/tcp/countme.projectbluefin.io/443 2>/dev/null && \
          printf 'GET %s HTTP/1.1\r\nHost: countme.projectbluefin.io\r\nConnection: close\r\n\r\n' \
          "${url#*://}" >&3; } || true
    fi

    printf 'countme: weekly ping sent (repo=%s, flavor=%s, bucket=%s)\n' \
        "${repo}" "${flavor}" "${bucket}" >&2
}

main "$@"
