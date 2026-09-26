#!/usr/bin/bash
# Make /var/home a labelled home root, so accounts-daemon can create users.
#
# Hummingbird's base image ships /etc/default/useradd with HOME=/var/home.
# genhomedircon builds the SELinux home rules from that value, and it drops
# /var/home as a home root because file_contexts.subs_dist already maps
# /var/home to /home. The result: /var/home matches no directory rule and is
# labelled default_t. useradd run by accounts-daemon (useradd_t) is then
# denied write on it:
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
# Do the same, rebuild the generated home rules, and fail the build if the
# labels are not what Initial Setup needs.
set -euo pipefail

sed -i 's|^HOME=/var/home$|HOME=/home|' /etc/default/useradd
grep -qx 'HOME=/home' /etc/default/useradd

# Regenerate file_contexts.homedirs from the new HOME root. -n: no policy
# reload; an image build has no selinuxfs to load it into.
semodule -n -B

want() {
    local path=$1 type=$2 got
    got=$(matchpathcon -m dir "$path" | awk '{print $2}')
    if [[ "$got" != *":${type}:"* ]]; then
        echo "ERROR: ${path} labels as ${got}, want ${type}; accounts-daemon cannot create users" >&2
        exit 1
    fi
    echo "${path}: ${got}"
}
want /var/home home_root_t
want /var/home/someone user_home_dir_t
