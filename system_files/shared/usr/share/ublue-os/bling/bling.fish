#!/usr/bin/env fish
# Compatibility shim: on Utah, bluefin-cli replaces Universal Blue's bling.
# config.fish files that still source this file get the bluefin-cli shell
# experience; `ujust bluefin-cli` replaces the old line with bluefin-cli's own.
if status is-interactive; and type -q bluefin-cli
    bluefin-cli init fish | source
end
