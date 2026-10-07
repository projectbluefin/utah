#!/usr/bin/bash
# Make /var/home a labelled home root, so accounts-daemon can create users.
#
# Hummingbird's base image ships /etc/default/useradd with HOME=/var/home.
# genhomedircon builds the SELinux home rules from that value, so they come
# out keyed on /var/home/[^/]+. But file_contexts.subs_dist maps /var/home to
# /home, and every lookup is rewritten to /home/... before it is matched, so
# the /var/home-keyed rules are never reached. The result: /var/home matches
# no directory rule and is labelled default_t. useradd run by accounts-daemon
# (useradd_t) is then denied write on it:
#
#   avc: denied { write } for comm="useradd" name="home"
#        scontext=system_u:system_r:useradd_t:s0
#        tcontext=system_u:object_r:default_t:s0 tclass=dir
#
# so GNOME Initial Setup fails with "Failed to run useradd: Child process
# exited with code 12" and an install ends with no account (#261).
#
# Fedora, and so Bluefin, keeps HOME=/home and relies on the /var/home -> /home
# substitution: /home -d is home_root_t and /home/[^/]+ is user_home_dir_t.
# Utah ships the same HOME=/home. Restoring Hummingbird's HOME=/var/home after
# generating the rules is not enough: ostree rebuilds the policy in every new
# deployment (`semodule -N --refresh` in ostree-finalize-staged, after the
# /etc merge, with that deployment's /usr writable), and that rebuild read
# HOME=/var/home and rewrote the active file_contexts.homedirs into the broken
# form on every update (#474, #575). The live ISO creates liveuser with an
# explicit --home-dir /var/home/liveuser, because in a container /var/home
# does not exist yet and `useradd -m` under /home trips on the dangling /home
# symlink (#281).
#
# --check runs at the image's last step: it rebuilds the policy exactly as
# ostree does on update and fails the build if the home rules change, so a
# later package install cannot ship an image whose next deployment breaks
# the home labels.
set -euo pipefail

homedirs=/etc/selinux/targeted/contexts/files/file_contexts.homedirs

want() {
    local path=$1 type=$2 got
    got=$(matchpathcon -m dir "$path" | awk '{print $2}')
    if [[ "$got" != *":${type}:"* ]]; then
        echo "ERROR: ${path} labels as ${got}, want ${type}; accounts-daemon cannot create users" >&2
        exit 1
    fi
    echo "${path}: ${got}"
}

if [[ "${1:-}" != "--check" ]]; then
    sed -i 's|^HOME=/var/home$|HOME=/home|' /etc/default/useradd
    # Regenerate file_contexts.homedirs from the /home root. -n: no policy
    # reload; an image build has no selinuxfs to load it into.
    semodule -n -B
fi

if ! grep -qx 'HOME=/home' /etc/default/useradd; then
    echo "ERROR: /etc/default/useradd does not say HOME=/home; the next policy rebuild would key the home rules on the wrong root" >&2
    exit 1
fi

if [[ "${1:-}" == "--check" ]]; then
    shipped=$(mktemp)
    cp -a "$homedirs" "$shipped"
    # The same rebuild ostree runs in each new deployment.
    semodule -N --refresh
    if ! cmp -s "$shipped" "$homedirs"; then
        echo "ERROR: a policy rebuild changes ${homedirs}; every update would rewrite the home labels:" >&2
        diff "$shipped" "$homedirs" | head -n 5 >&2 || true
        rm -f "$shipped"
        exit 1
    fi
    rm -f "$shipped"
fi

want /var/home home_root_t
want /var/home/someone user_home_dir_t
