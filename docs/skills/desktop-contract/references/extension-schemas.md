# Extension schemas are global; overrides from common must resolve

Common sets extension defaults in
`zz0-bluefin-modifications.gschema.override`, among them
`[org.gnome.shell.extensions.dash-to-dock] disable-overview-on-startup=true`.
The build used to compile each extension's schemas inside its own
`<uuid>/schemas/`, so the Containerfile's global `glib-compile-schemas` never
saw them and dropped the dash-to-dock section and two blur-my-shell sections
(`.dash-to-dock`, `.popup`). glib 2.90 prints nothing and exits 0 when an
override names a missing schema, so the build stayed green and GNOME opened
the Activities overview at login. `Extension.getSettings()` also prefers an
extension's own `schemas/` over the system directory when it exists, so a
global copy beside the local one would still be masked. Fedora's extension
RPMs install the schema globally and ship no local directory, and so do the
factory's recipes for Bluefin's extensions; Utah installs every extension but
GSConnect from those RPMs (#1235).

The rule: every extension schema is installed in `/usr/share/glib-2.0/schemas`
and no extension keeps a `schemas/` directory; every override section must
name a schema installed there.

## Enforcement

- The extension RPMs install each `*.gschema.xml` into the system directory
  and ship no `<uuid>/schemas/` (the factory recipes' `%check` asserts it).
  GSConnect's meson install, the one extension still built in the image, also
  uses the system directory.
- Installed-mode `scripts/verify-gnome-extensions.py` fails if any
  `<uuid>/schemas` remains.
- `[gsettings]` in `contracts/bluefin-desktop.toml`, checked by
  `scripts/verify-desktop-contract.py` after the global compile (and by
  `just check-desktop-contract`), fails the build when an override section
  (`[schema]` or `[schema:DESKTOP]`) in `*.gschema.override` names no schema a
  `*.gschema.xml` there declares. It also fails when two files declare one
  schema id: glib ignores the second file and still exits 0.
- `[gsettings.unresolved_overrides]` is the commented allowlist, one reason
  per schema. An entry that becomes installed, or that no override names any
  more, fails too, so the list cannot go stale:
  - `org.gnome.software`: the base manifest excludes GNOME Software
    (`packages/base.toml` `[excluded]`), Dakota does not ship it, and Utah
    ships Bazaar.
  - `com.github.stunkymonkey.nautilus-open-any-terminal`: neither
    `packages/base.toml` nor Dakota ships the package, so there is
    no parity gap and no `[unavailable]` entry to track.
- `[gsettings.values]` asserts what `gsettings get SCHEMA KEY` prints in the
  composed image: dash-to-dock's `disable-overview-on-startup` is `true`, and
  `org.gnome.shell enabled-extensions` is exactly Dakota's default set.

Tests: `GSettingsTests` in `tests/test_desktop_contract_verify.py` (fixtures in
`tests/fixtures/gsettings/`) and `InstalledExtensionTests` in
`tests/test_gnome_extensions.py`.

## Checking an image or a machine

```bash
gsettings list-schemas | grep -x org.gnome.shell.extensions.dash-to-dock
gsettings get org.gnome.shell.extensions.dash-to-dock disable-overview-on-startup  # true
ls -d /usr/share/gnome-shell/extensions/*/schemas  # nothing
```

A user's own dconf value still wins over the override; `gsettings reset` the
key to see the image default.
