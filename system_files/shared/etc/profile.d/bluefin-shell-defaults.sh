# shellcheck shell=sh
# Default Bluefin shell MOTD to disabled, while allowing user overrides
# (projectbluefin/dakota elements/bluefin/common.bst).
: "${BLUEFIN_SHELL_ENABLE_MOTD:=0}"
export BLUEFIN_SHELL_ENABLE_MOTD
