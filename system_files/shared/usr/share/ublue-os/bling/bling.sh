#!/usr/bin/env sh
# shellcheck shell=sh
# Compatibility shim: on Utah, bluefin-cli replaces Universal Blue's bling.
#
# ublue-bling wrote `source /usr/share/ublue-os/bling/bling.sh` into ~/.bashrc
# and ~/.zshrc, and people copied that line by hand. This file keeps those rc
# files working by running the bluefin-cli shell experience instead, which
# carries bling's aliases and hooks (eza, ugrep, bat, open, direnv, starship,
# zoxide, mise). `ujust bluefin-cli`, or `bluefin-cli shell <shell> on|off`,
# replaces the old line with bluefin-cli's own.
#
# KEEP THIS POSIX: bash and zsh source it.
if command -v bluefin-cli >/dev/null 2>&1; then
    eval "$(bluefin-cli init "$(basename "$(readlink /proc/$$/exe)")")"
fi
