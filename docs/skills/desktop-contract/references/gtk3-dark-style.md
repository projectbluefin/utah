## GTK3 dark style (#1342)

GTK3 reads `org.gnome.desktop.interface gtk-theme`, never `color-scheme`. With
the stock `Adwaita` value, GTK3 apps stayed light in Dark mode even though
`adw-gtk3-theme` was installed. Dakota ships no GTK3 theme and has no
equivalent. Utah follows Bluefin (ublue-os/bluefin#5163 and
ublue-os/bluefin-lts#2234). projectbluefin/common has no copy of this.

- `zz3-bluefin-unsupported-stuff.gschema.override` defaults `gtk-theme` to
  `adw-gtk3`. Common's `zz0` override sets other keys in the same schema, and
  zz3 sorts after it.
- `ublue-gtk3-theme-sync.service` is a user unit,
  `WantedBy=graphical-session.target`, that skips greeter sessions. The user
  preset and `configure-services.sh` (`systemctl --global enable`) enable it,
  and the build fails if the unit is missing. It runs
  `/usr/libexec/ublue-gtk3-theme-sync`, which is Bluefin's script with a Utah
  header. Keep the two in step.
- The script sets `adw-gtk3-dark` on `prefer-dark` and `adw-gtk3` otherwise.
  It runs once at login, then on every `color-scheme` change through
  `gsettings monitor`. It acts only while the theme is one of those two, so a
  theme the user picked by hand (Adwaita, HighContrast, anything else) stays.
- Flatpak GTK3 apps read the `org.gtk.Gtk3theme.adw-gtk3` and `-dark`
  extensions, which are already in `[flatpak].apps`.

The contract pins the default (`[gsettings.values]`), the unit
(`[services].user_enabled`), and the script, unit and both theme files
(`[configuration].files`). `tests/test_gtk3_theme_sync.py` runs the script
against a stand-in `gsettings`.
