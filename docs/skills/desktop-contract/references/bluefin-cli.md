## Terminal experience: bluefin-cli and uwelcome

[bluefin-cli](https://github.com/tuna-os/bluefin-cli) owns Utah's shell
experience: shell tools and prompt hooks, and the command-line Homebrew
bundles. It replaces Common's `ublue-bling` and `bling.sh`/`bling.fish`.
uwelcome stays the terminal greeting, and bluefin-cli works through it.
projectbluefin/common#1719 proposes the same move for every Bluefin image,
with Utah as the reference.

| Before (Common and Dakota) | On Utah |
| --- | --- |
| `ublue-bling` | Shim that runs `ujust bluefin-cli` |
| `source /usr/share/ublue-os/bling/bling.sh` / `bling.fish` in rc files | The same files are shims that run `bluefin-cli init <shell>`; `bluefin-cli shell <shell> on\|off` replaces the old line |
| `ujust bluefin-cli` (Dakota's `system.just`: `ublue-bling` + `cli.Brewfile`) | Overridden in `60-custom.just`: `bluefin-cli shell <shell> on` + `bluefin-cli install cli`, or `off` when the rc file has bluefin-cli's line |
| uwelcome greeting, `ujust toggle-user-motd` | Unchanged: Common's `uwelcome.sh`, `fish_greeting.fish` and `toggle-user-motd` |

- **Greeting.** uwelcome greets as on every Bluefin image (Utah's
  interactive-shell guard on `uwelcome.sh` is Dakota parity, #675).
  Dakota's `bluefin-shell-defaults.sh` keeps bluefin-cli's own MOTD off.
  Where uwelcome is installed, `bluefin-cli motd show|toggle` drive uwelcome
  (`~/.config/uwelcome/disabled`). bluefin-cli's init script greets only
  through uwelcome and stays quiet once `UWELCOME_SHOWN` is set, so a terminal
  is never greeted twice.
- **Removed from Common's files** by `configure-services.sh`: the bling
  helpers only Common's `bling.sh` and `ublue-bling` read (`env.sh`,
  `bash-preexec-rearm.sh`). bluefin-cli's init script keeps bash-preexec's
  DEBUG trap armed itself.
- **Package.** utah-packages builds the RPM (`packages/bluefin-cli`) from the
  release tag plus the release's vendored Go modules (utah-packages#1235);
  `packages/utah.toml` installs it from `[parity]` and `[factory].parity`. A
  `/usr/bin` install never updates itself; bluefin-cli updates with the image.
- **Tests.** `tests/test_ujust_overrides.py` (the recipe) and
  `tests/test_desktop_contract.py` (shims and removals).
