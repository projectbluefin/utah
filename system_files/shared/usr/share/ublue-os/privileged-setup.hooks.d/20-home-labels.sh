#!/usr/bin/bash
# Relabel /var/home once on systems installed before the image fixed its
# home-root label (scripts/fix-home-labels.sh, #261), and repair a second
# failure mode where the *active* file_contexts.homedirs disagrees with the
# image's own default after a switch (#474).
#
# file_contexts.subs_dist aliases /var/home -> /home: every selabel_lookup
# for /var/home/<user> is rewritten to /home/<user> before it is matched.
# The image ships a /home-keyed file_contexts.homedirs, which the alias
# reaches from either root. A machine that switched into Utah from an image
# which generated file_contexts.homedirs from HOME=/var/home -- or that
# carried an /etc override across the switch -- ends up with an *active*
# copy keyed on /var/home instead: a rule the alias never reaches. restorecon
# then finds nothing matching the wrong-rooted rules and is a silent no-op --
# /var/home and every home directory stay default_t even though the hook
# records success.
#
# Detect the mismatch by comparing the active homedirs file against the
# image's own pristine copy under /usr/etc, and reinstall the pristine one
# when the active copy is keyed on the wrong root. Then check the real
# on-disk label after restorecon rather than trusting its exit code, since a
# mismatched database lets it finish successfully without relabelling
# anything.

# shellcheck source=/dev/null
source /usr/lib/ublue/setup-services/libsetup.sh

# Compat shim: common libsetup.sh builds older than projectbluefin/common #1196
# have only version-script, which records the version before the body runs, and
# no version-script-check/version-script-commit pair. Fall back to that legacy
# gate and make the commit a no-op, so this hook works against both contracts.
if ! declare -F version-script-check >/dev/null; then
    version-script-check() { version-script "$@"; }
    version-script-commit() { :; }
fi

# Bumped from 1 to 2: machines that ran the version-1 hook before #474 was
# understood recorded success even when restorecon was a no-op against a
# mis-keyed homedirs database, so they must retry once under this contract.
version-script-check home-labels privileged 2 || exit 0

set -xeuo pipefail

active_homedirs=/etc/selinux/targeted/contexts/files/file_contexts.homedirs
pristine_homedirs=/usr/etc/selinux/targeted/contexts/files/file_contexts.homedirs

# If the active homedirs file is keyed on /var/home while the image's own
# default is keyed on /home, the active copy is unreachable through the
# file_contexts.subs_dist alias and restorecon cannot repair anything below:
# reinstall the pristine pair first.
if [[ -f "$active_homedirs" && -f "$pristine_homedirs" ]] \
    && grep -q '^/var/home/\[' "$active_homedirs" \
    && grep -q '^/home/\[' "$pristine_homedirs"; then
    cp -a "$pristine_homedirs" "$active_homedirs"
    if [[ -f "${pristine_homedirs}.bin" ]]; then
        cp -a "${pristine_homedirs}.bin" "${active_homedirs}.bin"
    fi
fi

restorecon -RF /var/home

# restorecon's exit code does not tell us the labels are actually correct: a
# database still keyed on the wrong root would have let it finish with
# nothing to relabel. Check the real on-disk context before committing, so a
# machine that is still mislabelled retries next boot instead of being
# marked done forever.
home_root_context=$(stat -c '%C' /var/home)
if [[ "$home_root_context" != *:home_root_t:* ]]; then
    echo "ERROR: /var/home labels as ${home_root_context}, want home_root_t" >&2
    exit 1
fi

version-script-commit home-labels privileged 2
