## Dakota's edits to Common (#1266)

Dakota (`elements/bluefin/common.bst`) changes projectbluefin/common's files
before it installs them. Utah copies Common unedited and applies the same
edits afterwards, so the composed image matches what Dakota actually ships.
`configure-services.sh` guards each one: already applied is a no-op, and an
upstream file of a different shape fails the build. `DakotaCommonEditTests`
in `tests/test_desktop_contract.py` runs the blocks against Common-shaped and
foreign input.

- **Monthly wallpaper.** Utah ships both of Dakota's month units:
  `ublue-wallpaper-month.service` (`bluefin/wallpaper-month.bst`) links
  `/var/lib/ublue-os/current-bluefin.xml`, and `bluefin-wallpaper-month.service`
  with `/usr/libexec/bluefin-wallpaper-month-sync` (`files/wallpaper-month`)
  links `/var/lib/bluefin/backgrounds/current-bluefin.xml`. Both are enabled
  in the preset, `configure-services.sh` and the contract. Nothing reads
  either link. Dakota's sed repoints zz0 from `12-bluefin.xml`, but Common
  names another month, so the sed matches nothing
  (projectbluefin/dakota#1902). The default stays Common's zz0 value, and
  Common's per-user `bluefin-dynamic-wallpaper` timer keeps rotating. The
  build fails if Common's zz0 ever names `12-bluefin.xml`, because Dakota's
  repoint would then take effect and Utah has to follow it.
- **Weekly stats.** Dakota's `patches/common/0006-fastfetch-drop-weekly-stats.patch`
  removes the user-count and Bazaar-installs lines from `fastfetch.jsonc`,
  along with the break after them. Utah removes the same lines, and the
  result is byte-identical to the patched file. The runtime refresher
  `bluefin-stats-refresh` (#860) and its `bluefin-stats` user are gone.
- **Ghostty theme.** `patches/common/0002-ghostty-dual-theme.patch` sets
  `/etc/skel/.config/ghostty/config.ghostty` to
  `theme = light:Catppuccin Latte,dark:Catppuccin Mocha`, so Ghostty follows
  GNOME's light/dark style.
- **Logos.** os-release `LOGO=img-logo-icon` and
  `/usr/share/pixmaps/img-logo-icon{,-text}.svg` (light wordmark),
  `img-logo-icon{,-text}-dark.svg` (dark), fetched by the `artwork` stage
  from `config/artwork.lock` ([About artwork](about-artwork.md)).
- **Not applied:** `0004-policy-drop-redhat-gpg-entries`. Utah needs those
  entries to verify Hummingbird images. `0003-uwelcome-interactive-only` was
  already applied as the `uwelcome.sh` guard (#675).
