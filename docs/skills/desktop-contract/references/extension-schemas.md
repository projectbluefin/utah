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
RPMs install the schema globally and ship no local directory. Bluefin builds
dash-to-dock from source and has the same gap (its image has no global
dash-to-dock schema); its blur-my-shell comes from an RPM.

The rule: every extension schema is installed in `/usr/share/glib-2.0/schemas`
and no extension keeps a `schemas/` directory; every override section must
name a schema installed there.

## Enforcement

- `scripts/build-gnome-extensions.sh` ends by validating each
  `<uuid>/schemas/` with `glib-compile-schemas --strict --dry-run`, installing
  each `*.gschema.xml` into the system directory (it never overwrites an
  existing file), and removing the local directory with its compiled cache.
  The per-extension compiles are gone. GSConnect's meson install already uses
  the system directory. Quick Settings Audio Panel's bundled libpanel keeps a
  compiled schema in `libs/libpanel/` and loads it itself; that is not an
  extension `schemas/` directory and is left alone.
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
  - `org.gnome.software`: Bluefin excludes GNOME Software
    (`packages/bluefin.toml` `[excluded]`); Utah ships Bazaar.
  - `com.github.stunkymonkey.nautilus-open-any-terminal`: neither
    `packages/bluefin.toml` nor Bluefin's image ships the package, so there is
    no parity gap and no `[unavailable]` entry to track.
- `[gsettings.values]` asserts what `gsettings get SCHEMA KEY` prints in the
  composed image: dash-to-dock's `disable-overview-on-startup` is `true`.

Tests: `GSettingsTests` in `tests/test_desktop_contract_verify.py` (fixtures in
`tests/fixtures/gsettings/`) and `ExtensionSchemaTests` in
`tests/test_gnome_extensions.py`.

## Checking an image or a machine

```bash
gsettings list-schemas | grep -x org.gnome.shell.extensions.dash-to-dock
gsettings get org.gnome.shell.extensions.dash-to-dock disable-overview-on-startup  # true
ls -d /usr/share/gnome-shell/extensions/*/schemas  # nothing
```

A user's own dconf value still wins over the override; `gsettings reset` the
key to see the image default.
