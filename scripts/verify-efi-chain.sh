#!/usr/bin/env bash
# Refuse to build an image whose installed system could not boot: every shim
# must have its GRUB beside it.
#
# shim loads grubx64.efi from its own directory, and GRUB reads its config
# from the prefix compiled into it. bootupd installs package payloads from
# /usr/lib/efi/<component>/<version>/EFI/<vendor>/ onto the ESP and writes
# grub.cfg next to shim. So for every EFI/<vendor>/shimx64.efi there must be a
# package-owned EFI/<vendor>/grubx64.efi whose compiled prefix is
# /EFI/<vendor>.
#
# Utah shipped without that for days (post-testing install e2e red from
# 2026-09-21: "Failed to open \EFI\fedora\grubx64.efi - Not Found"). shim came
# from Fedora (EFI/fedora) and GRUB from the factory, built with Hummingbird's
# efi_vendor (EFI/hummingbird, prefix /EFI/hummingbird). Before that, a stale
# UNOWNED Fedora grubx64.efi mirrored beside shim hid the mismatch. That is
# why ownership is checked too: an unowned copy is not a fix, it is the mask.
set -euo pipefail

EFI_ROOT="${EFI_ROOT:-/usr/lib/efi}"

fail() {
    echo "ERROR: EFI boot chain: $*" >&2
    exit 1
}

mapfile -t shims < <(find "${EFI_ROOT}" -mindepth 5 -maxdepth 5 \
    -path "${EFI_ROOT}/*/*/EFI/*/shimx64.efi" 2>/dev/null | sort)
[[ ${#shims[@]} -gt 0 ]] || fail "no shimx64.efi under ${EFI_ROOT}/<component>/<version>/EFI/<vendor>/
  bootupd has nothing to install; the image cannot boot with Secure Boot or
  through shim at all."

all_grubs="$(find "${EFI_ROOT}" -name grubx64.efi 2>/dev/null | sort | tr '\n' ' ')"

for shim in "${shims[@]}"; do
    vendor_dir="$(dirname "${shim}")"
    vendor="$(basename "${vendor_dir}")"
    mapfile -t grubs < <(find "${EFI_ROOT}" -mindepth 5 -maxdepth 5 \
        -path "${EFI_ROOT}/*/*/EFI/${vendor}/grubx64.efi" 2>/dev/null | sort)
    [[ ${#grubs[@]} -gt 0 ]] || fail "${shim} loads grubx64.efi from EFI/${vendor}/, but no GRUB is packaged there.
  grubx64.efi found at: ${all_grubs:-nowhere}
  GRUB must be built with the same efi_vendor as the shim that loads it."

    owned=""
    for grub in "${grubs[@]}"; do
        if owner="$(rpm -qf "${grub}" 2>/dev/null)"; then
            owned="${grub}"
            break
        fi
    done
    [[ -n "${owned}" ]] || fail "EFI/${vendor}/grubx64.efi exists only as an unowned copy (${grubs[*]}).
  A copy no package owns is not updated with GRUB and hid this exact failure
  once already. GRUB must be packaged for EFI/${vendor}."

    if ! grep -aqF "/EFI/${vendor}" "${owned}"; then
        prefixes="$(grep -ao '/EFI/[A-Za-z0-9_.-]*' "${owned}" | sort -u | tr '\n' ' ')"
        fail "${owned} (${owner}) does not carry the prefix /EFI/${vendor}; it has: ${prefixes:-none}.
  It would start and then look for grub.cfg in the wrong directory."
    fi
    echo "EFI boot chain OK: ${shim} -> ${owned} (${owner}), prefix /EFI/${vendor}"
done
