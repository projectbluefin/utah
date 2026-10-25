#!/usr/bin/bash
# Move the SELinux module store from /usr/lib/selinux to /etc/selinux, where
# Fedora atomic and Bluefin keep it, so persistent SELinux changes work on a
# booted system (#551).
#
# Hummingbird's base image relocates the libsemanage store to
# /usr/lib/selinux and appends `store-root=/usr/lib/selinux` to
# /etc/selinux/semanage.conf, so that the store is image-managed and
# immutable. On a booted bootc system /usr is read-only, which means every
# tool that writes the store fails: `setsebool -P` exits 255 without a
# message, and `semanage` and `semodule` -- even `semodule -l` -- cannot take
# the store lock. Users cannot persist a boolean, add a file context or port
# label, or load a local module, all of which work on Bluefin.
#
# Under /etc the store is writable at runtime and ostree carries local
# changes across updates through its 3-way /etc merge. Image policy updates
# still reach every machine: ostree rebuilds the policy in each new
# deployment after the merge (`semodule -N --refresh`, ostree >= 2022.3),
# combining the new image's modules with the machine's local ones. A local
# module the new policy cannot satisfy fails that rebuild and ostree then
# does not apply the update; see docs/skills/containerfile.md.
#
# This runs during the image build, where /usr is writable, before any step
# that rebuilds the policy (scripts/fix-home-labels.sh), so the rebuild
# writes into the store's final location. The compiled policy that the
# kernel loads is unaffected: it already lives at /etc/selinux/targeted/.
#
# The first argument, if given, is the root to operate on; it defaults to /.
# Tests pass a scratch tree.
set -euo pipefail

root="${1:-}"
root="${root%/}"

old_store="${root}/usr/lib/selinux"
new_store="${root}/etc/selinux"
conf="${new_store}/semanage.conf"
old_line='store-root=/usr/lib/selinux'
new_line='store-root=/etc/selinux'

if [[ ! -f "$conf" ]]; then
    echo "ERROR: ${conf} is missing; cannot relocate the SELinux module store" >&2
    exit 1
fi

if grep -qx "$new_line" "$conf" && [[ ! -e "$old_store" ]]; then
    echo "SELinux module store already under /etc/selinux"
    exit 0
fi

if ! grep -qx "$old_line" "$conf"; then
    echo "ERROR: ${conf} does not carry '${old_line}'; the base image's store layout changed, review this script" >&2
    exit 1
fi

if [[ ! -d "${old_store}/targeted/active" ]]; then
    echo "ERROR: ${old_store}/targeted/active is missing; nothing to relocate" >&2
    exit 1
fi

if [[ -e "${new_store}/targeted/active" ]]; then
    echo "ERROR: ${new_store}/targeted/active already exists alongside ${old_store}; two stores cannot both be live" >&2
    exit 1
fi

mkdir -p "${new_store}/targeted"
mv "${old_store}/targeted/active" "${new_store}/targeted/active"
# libsemanage stages the compiled policy under <store-root>/final before
# installing it into /etc/selinux/<type>; the directory follows the store.
if [[ -d "${old_store}/final" ]]; then
    rm -rf "${new_store}/final"
    mv "${old_store}/final" "${new_store}/final"
fi
# libsemanage keeps its store locks at <store-root>/<type>/semanage.*.LOCK
# and (re)creates them empty on first use. The package transaction that
# precedes this script takes them, so they are present in the build and
# belong with the store.
for lock in semanage.read.LOCK semanage.trans.LOCK; do
    old_lock="${old_store}/targeted/${lock}"
    new_lock="${new_store}/targeted/${lock}"
    [[ -f "$old_lock" ]] || continue
    if [[ ! -e "$new_lock" ]]; then
        mv "$old_lock" "$new_lock"
    elif [[ ! -s "$old_lock" ]]; then
        rm -f "$old_lock"
    else
        echo "ERROR: ${old_lock} is not an empty lock file and ${new_lock} already exists" >&2
        exit 1
    fi
done

# Only the entries Hummingbird moved and libsemanage's locks may be left
# behind; anything else means the base changed under us and would be lost
# silently by an rm -rf.
rmdir "${old_store}/targeted" 2>/dev/null || true
leftovers=$(find "$old_store" -mindepth 1 -print 2>/dev/null | head -n 5 || true)
if [[ -n "$leftovers" ]]; then
    echo "ERROR: ${old_store} still holds unexpected entries after the move:" >&2
    echo "$leftovers" >&2
    exit 1
fi
rmdir "$old_store"

sed -i "s|^${old_line}\$|${new_line}|" "$conf"
if ! grep -qx "$new_line" "$conf" || grep -qx "$old_line" "$conf"; then
    echo "ERROR: failed to point ${conf} at ${new_line}" >&2
    exit 1
fi

echo "SELinux module store relocated to /etc/selinux ($(grep -x "$new_line" "$conf"))"
