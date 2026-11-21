#!/usr/bin/env bash
set -euo pipefail

max_attempts=5
for ((attempt = 1; attempt <= max_attempts; attempt++)); do
    if git submodule sync --recursive &&
        git -c submodule.fetchJobs=1 submodule update --init --recursive; then
        exit 0
    fi
    if (( attempt == max_attempts )); then
        echo "submodule checkout failed after ${max_attempts} attempts" >&2
        exit 1
    fi
    delay=$((attempt * 10))
    echo "submodule checkout failed; retrying in ${delay}s (${attempt}/${max_attempts})" >&2
    sleep "$delay"
done
