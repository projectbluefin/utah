# Desktop and GNOME extensions

The installed desktop contract is `/usr/share/utah/bluefin-desktop.toml`.
Utah vendors extensions as pinned Git submodules and builds them with
`utah-build-gnome-extensions` during image composition; some extensions are
checksum-verified release archives. They need not be owned by an RPM.
An extension's `metadata.json` version is a release marker, not its source
commit. Use the image's Utah commit, `.gitmodules`, and `git ls-tree` in a
checkout to recover the exact gitlink; archive URLs and checksums are in the
build script. The bundled source snapshots describe both mechanisms.

For a reversible per-user override, copy the installed UUID directory from
`/usr/share/gnome-shell/extensions/` into
`~/.local/share/gnome-shell/extensions/` (create the parent first). Modify
that copy, preserving the UUID and `metadata.json`. If it has schemas, run
`glib-compile-schemas ~/.local/share/gnome-shell/extensions/UUID/schemas`.
The user copy takes precedence at the next Shell start. On Wayland, log out
and back in after saving work; restarting GDM terminates sessions.
Check `gnome-extensions info UUID`, then the journal; if the state is ERROR
and the journal is silent, ask the shell directly: `gdbus call --session
--dest org.gnome.Shell.Extensions --object-path /org/gnome/Shell/Extensions
--method org.gnome.Shell.Extensions.GetExtensionErrors UUID`.
Remove the user copy and log in again to restore the image version.

Inspect `gsettings get org.gnome.shell disable-extension-version-validation`:
Utah disables version validation, so loading is not proof of compatibility.
Do not write into `/usr` or infer compatibility from the declared versions.
Permanent changes require a Utah checkout, updated source pins/build inputs,
and a rebuilt image; the build helper is not a runtime repair tool.
