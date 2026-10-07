#!/usr/bin/bash
# Relabel /var/home once on systems installed before the image fixed its
# home-root label (scripts/fix-home-labels.sh, #261), and repair a second
# failure mode where the *active* file_contexts.homedirs disagrees with the
# image's own default after a switch (#474) or an update (#575).
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
# Bumped from 2 to 3: until the image shipped HOME=/home in
# /etc/default/useradd, the policy rebuild ostree runs in every new
# deployment re-keyed the active homedirs on /var/home after the version-2
# repair (#575). Images that ship HOME=/home no longer do; run the repair
# once more for machines an earlier update broke again.
version-script-check home-labels privileged 3 || exit 0

set -xeuo pipefail

active_homedirs=/etc/selinux/targeted/contexts/files/file_contexts.homedirs
pristine_homedirs=/usr/etc/selinux/targeted/contexts/files/file_contexts.homedirs
useradd_defaults=/etc/default/useradd

# The image fix for #575 reaches /etc/default/useradd only through the 3-way
# /etc merge. A machine that ever edited that file keeps its whole local copy,
# including Hummingbird's HOME=/var/home, and every later deployment's policy
# rebuild re-keys the active homedirs on /var/home again after this hook has
# committed. Exactly HOME=/var/home is that stale default, not a choice (/home
# is a symlink to /var/home), so rewrite just that line; leave any other value
# alone but say why the labels will break again.
if [[ -f "$useradd_defaults" ]] && ! grep -qx 'HOME=/home' "$useradd_defaults"; then
    if grep -qx 'HOME=/var/home' "$useradd_defaults"; then
        echo "home-labels: ${useradd_defaults} kept HOME=/var/home across the /etc merge; setting HOME=/home (#575)" >&2
        sed -i 's|^HOME=/var/home$|HOME=/home|' "$useradd_defaults"
    else
        echo "WARNING: ${useradd_defaults} does not set HOME=/home; the next deployment's SELinux policy rebuild may re-key home labels on another root (#575)" >&2
    fi
fi

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

version-script-commit home-labels privileged 3
