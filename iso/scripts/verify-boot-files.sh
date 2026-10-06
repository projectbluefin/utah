#!/usr/bin/env bash
# Run as root in the guest: BLS paths are relative to their entry's filesystem,
# not the guest root or the harness host. Optional roots support fixture tests.
set -euo pipefail
shopt -s nullglob
if [[ $# -eq 0 ]]; then
    set -- /boot /boot/efi /efi /
fi

checked=0
failed=0
for root in "$@"; do
    root="${root%/}"
    for entry in "${root}"/loader/entries/*.conf; do
        linux=()
        initrd=()
        ostree=0
        while read -r key value; do
            case "$key" in
                linux) linux+=("$value") ;;
                initrd) initrd+=("$value") ;;
                options)
                    if [[ " $value " == *" ostree="* ]]; then
                        ostree=1
                    fi
                    ;;
            esac
        done < "$entry"
        # Other operating systems and UKIs are outside this bootc Type #1 gate.
        [[ "$ostree" == 1 ]] || continue
        checked=$((checked + 1))
        printf 'ENTRY %s\n' "$entry"
        if [[ ${#linux[@]} -eq 0 || ${#initrd[@]} -eq 0 ]]; then
            echo 'FAIL: missing linux or initrd directive'
            failed=1
        fi
        for path in "${linux[@]}" "${initrd[@]}"; do
            # Do not allow a path to escape the entry's ESP/XBOOTLDR root.
            if [[ "$path" != /* || "/${path#/}/" == *'/../'* ]]; then
                printf 'FAIL: invalid boot path %s\n' "$path"
                failed=1
            elif [[ ! -f "${root}${path}" || ! -s "${root}${path}" ]]; then
                printf 'FAIL: missing or empty boot file %s\n' "${root}${path}"
                failed=1
            else
                printf 'PASS: %s\n' "${root}${path}"
            fi
        done
    done
done
if [[ "$checked" == 0 ]]; then
    echo 'FAIL: no OSTree BLS Type #1 entries found'
    failed=1
fi
exit "$failed"
