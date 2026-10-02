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
#
# Hands off anything the user already arranged themselves: a per-app path that
# is already a symlink somewhere else is their own version of this fix, and
# both paths may be symlinks onto a common dotfiles directory. Rewriting
# either would reintroduce the ignored-config bug.
#
# First-boot-only: common#1196 split `version-script` into a read-only check
# (`version-script-check`) and a commit (`version-script-commit`) so a failed
# first-boot hook retries next boot rather than being skipped forever after.
# That matters here: the body moves real config around, and a body that aborts
# part-way must get another attempt instead of burning the stamp.

# shellcheck source=/dev/null
source /usr/lib/ublue/setup-services/libsetup.sh

# Compat shim: common libsetup.sh builds older than projectbluefin/common#1196
# have only version-script, which records the version before the body runs, and
# no version-script-check/version-script-commit pair. Fall back to that legacy
# gate and make the commit a no-op, so this hook works against both contracts.
if ! declare -F version-script-check >/dev/null; then
    version-script-check() { version-script "$@"; }
    version-script-commit() { :; }
fi

version-script-check 30-ghostty user 1 || exit 0

set -euo pipefail

app_id="com.mitchellh.ghostty"
host_dir="${XDG_CONFIG_HOME:-${HOME}/.config}/ghostty"
sandbox_parent="${HOME}/.var/app/${app_id}/config"
sandbox_dir="${sandbox_parent}/ghostty"

mkdir -p "${sandbox_parent}"

host_target="$(readlink -f "${host_dir}" 2>/dev/null || true)"
sandbox_target="$(readlink -f "${sandbox_dir}" 2>/dev/null || true)"

# The per-app path is already a symlink aimed somewhere that is not the host
# config directory: the user wired it up themselves (dotfiles, say). That is
# the same fix by hand, so there is nothing to repair -- and the work below
# would undo it, creating an empty ~/.config/ghostty, seeding the default theme
# into it and re-pointing the per-app path there, which is exactly the "config
# silently ignored" bug this hook exists to fix.
#
# Leave without committing: if the user ever removes their link, a later boot
# gets another chance to set this up. (Under the pre-1196 compat shim the
# version is already recorded by the check, so that retry only happens on
# libsetup builds that have the check/commit split -- there is nothing this
# hook can do about the legacy gate, and doing nothing is still correct.)
if [[ -L "${sandbox_dir}" && "${sandbox_target}" != "${host_target}" ]]; then
    exit 0
fi

# The reverse of what this hook builds: ~/.config/ghostty is itself a symlink
# into the per-app directory, which is a plausible hand-rolled workaround for
# the same bug. Treating that as "a real per-app config to migrate" would copy
# the directory onto itself, move the only real config aside, and leave the
# host symlink dangling. Put the real directory back at the host path instead.
#
# Only when the per-app path holds the real files. If both paths are symlinks
# onto a common third directory, resolved-target equality also holds, and
# removing the host link there would delete a user-managed link while the `mv`
# below declined to put anything back -- losing both links to the real config.
if [[ ! -L "${sandbox_dir}" && -L "${host_dir}" && -n "${host_target}" && "${host_target}" == "${sandbox_target}" ]]; then
    rm -f "${host_dir}"
    if [[ -d "${sandbox_dir}" ]]; then
        mv "${sandbox_dir}" "${host_dir}"
    fi
fi

mkdir -p "${host_dir}"

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
# Ghostty configuration. Ghostty ships only as a flatpak here, so the defaults
# are `flatpak run com.mitchellh.ghostty +show-config --default --docs`.
# Follows the system light/dark style; change either half to taste.
theme = light:Adwaita,dark:Adwaita Dark
CFG
fi

ln -sfn "${host_dir}" "${sandbox_dir}"

# Record success only after the body ran, so a failing first-boot hook retries
# next boot instead of being permanently skipped (common#1196 new contract).
version-script-commit 30-ghostty user 1
