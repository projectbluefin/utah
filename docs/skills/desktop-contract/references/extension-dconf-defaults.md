# Extension dconf defaults must match installed schema paths

`/etc/dconf/db/distro.d/08-dakota-extension-defaults` (and common's
`04-bluefin-custom-command-menu`) set defaults for shell extensions. The
extension RPMs install their GSettings schemas in `/usr/share/glib-2.0/schemas`
([extension schemas](extension-schemas.md)), and GSettings resolves a key by the
schema's declared `path`, not by the extension's UUID folder name. A dconf
section header therefore only takes effect when it equals the `path=` of a
schema the image actually installs.

The headers use the short schema path, never the UUID folder:

- `org/gnome/shell/extensions/tilingshell` (`gnome-shell-extension-tilingshell`)
- `org/gnome/shell/extensions/BudsLink-Companion` (`gnome-shell-extension-budslink-companion`)
- `org/gnome/shell/extensions/syncthing-toggle` (`gnome-shell-extension-syncthing-toggle`)
- `org/gnome/shell/extensions/blur-my-shell/popup` (a sub-path in `gnome-shell-extension-blur-my-shell`)

A header that names a path no schema declares is silently ignored: the key
falls back to the extension's own compiled default, so the value written here
does nothing. That was the original complaint in issue #754 — the keys looked
like they were written to orphan `@domain` paths. They were not: GSettings
reads the schema `path`, and every shipped schema declares the short path the
file already uses.

## Enforcement

The schemas come from RPMs, so the check runs against the composed image:
installed-mode `scripts/verify-gnome-extensions.py` reads every `path=` declared
in `/usr/share/glib-2.0/schemas/*.gschema.xml` and fails the build if any
`org/gnome/shell/extensions/*` section of a keyfile in
`/etc/dconf/db/distro.d` matches none of them (both sides normalized by
stripping surrounding slashes). `InstalledExtensionTests` in
`tests/test_gnome_extensions.py` covers it with fixtures.

Keep the invariant true when touching either side: if an extension's schema
`path=` moves, or a new extension default is added, the matching section header
must move with it or the image build fails instead of shipping a setting that
never applies.
