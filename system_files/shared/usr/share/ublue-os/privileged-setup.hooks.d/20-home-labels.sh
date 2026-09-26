#!/usr/bin/bash
# Relabel /var/home once on systems installed before the image fixed its
# home-root label (scripts/fix-home-labels.sh, #261). There, /var/home and
# every home directory were created as default_t, so accounts-daemon could not
# add users and home directories carried the wrong type. /var persists across
# updates, so a new image alone does not correct existing labels.

# shellcheck source=/dev/null
source /usr/lib/ublue/setup-services/libsetup.sh

version-script home-labels privileged 1 || exit 0

set -x
restorecon -RF /var/home
