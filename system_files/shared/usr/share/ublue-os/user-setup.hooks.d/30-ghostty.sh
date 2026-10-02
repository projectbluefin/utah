#!/usr/bin/bash
# Make the Ghostty flatpak read ~/.config/ghostty, and default it to following
# the system light/dark preference.
#
# The TunaOS build (com.mitchellh.ghostty) is granted only filesystems=home:ro.
# Inside the sandbox XDG_CONFIG_HOME is ~/.var/app/com.mitchellh.ghostty/config,
# so Ghostty reads ~/.var/app/com.mitchellh.ghostty/config/ghostty/config and
# never sees ~/.config/ghostty/config -- the file Ghostty's own docs, and every
# user, edit. Edits there were silently ignored.
#
# Pointing the per-app directory at ~/.config/ghostty fixes that without
# widening the sandbox: home:ro already exposes the target, and Ghostty only
# reads its config. A flatpak override cannot do it instead, because
# XDG_CONFIG_HOME would need the user's home in it and --env does not expand.

# shellcheck source=/dev/null
source /usr/lib/ublue/setup-services/libsetup.sh

version-script 30-ghostty user 1 || exit 0

set -euo pipefail

app_id="com.mitchellh.ghostty"
host_dir="${XDG_CONFIG_HOME:-${HOME}/.config}/ghostty"
sandbox_parent="${HOME}/.var/app/${app_id}/config"
sandbox_dir="${sandbox_parent}/ghostty"

mkdir -p "${host_dir}" "${sandbox_parent}"

# A config already written where the flatpak looked is the user's real config:
# keep it, without overwriting anything already in ~/.config/ghostty.
if [[ -d "${sandbox_dir}" && ! -L "${sandbox_dir}" ]]; then
    cp -a --update=none "${sandbox_dir}/." "${host_dir}/"
    mv "${sandbox_dir}" "${sandbox_dir}.utah-migrated-$(date +%s)"
fi

# Follow the GNOME light/dark preference. Ghostty switches between the two
# halves of a light:/dark: theme pair live, through the settings portal.
if [[ ! -e "${host_dir}/config" && ! -e "${host_dir}/config.ghostty" ]]; then
    cat > "${host_dir}/config" <<'CFG'
# Ghostty configuration. See `ghostty +show-config --default --docs`.
# Follows the system light/dark style; change either half to taste.
theme = light:Adwaita,dark:Adwaita Dark
CFG
fi

ln -sfn "${host_dir}" "${sandbox_dir}"
