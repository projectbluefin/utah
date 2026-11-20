## Dakota desktop defaults

Utah takes these defaults from `projectbluefin/dakota` (branch `testing`).
Paths below are in the Dakota repository.

### Enabled extensions

`zz3-bluefin-unsupported-stuff.gschema.override` enables exactly Dakota's set,
in Dakota's order (`elements/bluefin/shell-extensions/disable-ext-validator.bst`):
Caffeine, AppIndicator, Bazaar integration, Blur my Shell, Dash to Dock, Fuzzy
App Search, Gradia, GSConnect, Custom Command List, BudsLink-Companion and
Tiling Shell. The audio-device hider and renamer, Tailscale QS, both Syncthing
toggles, Power Status Color and Bluefin Light Style stay installed but opt-in.
Utah installs two extensions Dakota lacks: Bluefin Light Style (kept by owner
decision, #1235) and the rehhouari Syncthing toggle (#1357). A user who has
changed their extension list keeps it; an untouched profile follows the
default.

`[gsettings.values]` in `contracts/bluefin-desktop.toml` pins the exact list
`gsettings get org.gnome.shell enabled-extensions` prints in the image.
`GnomeExtensionTests` (`tests/test_desktop_contract.py`) keeps the override,
the contract and the shipped UUIDs in step.

### Installed extensions

The installed set follows Dakota's `elements/bluefin/gnome-shell-extensions.bst`
(#1235): Copyous and Quick Settings Audio Panel were dropped. `tmp/bluefin-bling`
is pinned to Dakota's `elements/bluefin/shell-extensions/power-status-color.bst`
ref, and the build installs every bling member: power-status-color and
`syncthing-toggle@projectbluefin.io` as Dakota does, plus
`light-style@projectbluefin.io`, a deliberate Utah addition. Bling discovery is
automatic, so a new bling member ships unless the build excludes it; check it
against Dakota when bumping the pin. `baselines/triage.toml` records only Dakota
items Utah lacks, so Utah-only extensions carry no entry there.

### Tiling Shell layouts

`08-dakota-extension-defaults` copies `layouts-json` from Dakota's
`files/dconf/08-dakota-extension-defaults`. Quarters has four distinct
quadrants; Utah once shipped three copies of the bottom-right tile. The
contract's `file_contains` pins the four quadrants.

### BudsLink Flatpak

BudsLink backs the default-enabled BudsLink-Companion extension. Utah ships
`/usr/share/flatpak/preinstall.d/io.github.maniacx.BudsLink.preinstall`
(Dakota: `files/oci/bluefin-flatpak/preinstall.d/`), and
`configure-services.sh` appends `flatpak "io.github.maniacx.BudsLink"` to
Common's `system-flatpaks.Brewfile`, as `elements/bluefin/common.bst` does. It
is therefore the last entry of the ordered `[flatpak].apps` contract.

### Common post-processing

As in `elements/bluefin/common.bst`:

- `configure-services.sh` removes Common's `system-update.desktop`,
  `discourse.desktop` and `documentation.desktop`. Control Center covers
  updates and the Community link, `ujust update` stays, and the Documentation
  launcher opened a PDF Utah does not ship.
- `/etc/profile.d/bluefin-shell-defaults.sh` defaults
  `BLUEFIN_SHELL_ENABLE_MOTD=0` (user-overridable); the contract asserts both
  lines.
