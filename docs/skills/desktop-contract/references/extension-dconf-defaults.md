# Extension dconf defaults must match shipped schema paths

`system_files/shared/etc/dconf/db/distro.d/08-dakota-extension-defaults`
sets defaults for shell extensions. The build installs their GSettings
schemas in `/usr/share/glib-2.0/schemas` ([extension schemas](extension-schemas.md)),
and GSettings resolves a key by the
schema's declared `path`, not by the extension's UUID folder name. A dconf
section header therefore only takes effect when it equals the `path=` of a
schema the extension actually ships.

The headers here use the short schema path, never the UUID folder:

- `org/gnome/shell/extensions/tilingshell` (folder `tmp/tilingshell@ferrarodomenico.com`)
- `org/gnome/shell/extensions/BudsLink-Companion` (folder `BudsLink-Companion@maniacx.github.com`)
- `org/gnome/shell/extensions/syncthing-toggle` (schema inside `tmp/bluefin-bling`)
- `org/gnome/shell/extensions/blur-my-shell/popup` (a sub-path in `blur-my-shell@aunetx`)

A header that names a path no schema declares is silently ignored: the key
falls back to the extension's own compiled default, so the value written here
does nothing. That was the original complaint in issue #754 — the keys looked
like they were written to orphan `@domain` paths. They were not: GSettings
reads the schema `path`, and every shipped schema declares the short path the
file already uses, so the file is correct.

The guard that replaced the debate is
`tests/test_dakota_extension_defaults.py`. It reads every `.gschema.xml` the
extensions ship (the submodules must be initialized, which `just check` does
with `git submodule update --init --recursive`), collects the `path=` each one
declares, and fails if any section header in `08-dakota-extension-defaults`
does not match one of them. Both sides are normalized by stripping surrounding
slashes, because a schema path is `/org/gnome/shell/extensions/tilingshell/`
while the section header is `org/gnome/shell/extensions/tilingshell`. The test
skips cleanly when the extension submodules are not checked out, so it enforces
the invariant in CI without requiring a submodule clone in every editor.

Keep the invariant true when touching either side: if an extension's schema
`path=` moves, or a new extension default is added, the matching section header
must move with it or the build fails here instead of shipping a setting that
never applies.
