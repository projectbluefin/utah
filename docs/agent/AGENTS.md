# Utah on-device agent context

Start here on a running Utah machine. This bundle is installed at
`/usr/share/utah/agent/` from the same source tree as the image. It is guidance,
not permission to change a machine. Collect evidence first and ask the owner
before interrupting sessions, rebooting, rolling back, or changing images.

Utah builds the Bluefin GNOME desktop on Hummingbird's `os-minimal` base.
The Utah package factory supplies the Bluefin package contract; tracked gaps
are explicit `[unavailable]` exceptions, and `[parity]` records provisions.
The manifests and desktop contract are one directory above this bundle.
This is experimental software; report reproducible failures with the image
version and flavor.

Read [the skill router](SKILL.md), [catalog](index.json), and
[source map](repo-map.md). Start diagnosis with
[diagnosis](skills/diagnosis.md); for extensions use
[desktop-contract](skills/desktop-contract.md).

The root filesystem is read-only by design (composefs). Do not edit `/usr`,
remount the root to repair it, or enable Fedora package repositories.
Use `/etc` for supported administrator configuration, `/var` for state and
home directories for user overrides. Permanent OS changes require a source
checkout, review and a rebuilt image. CI, kernel-cache and local-build skills
are deliberately excluded; obtain the checkout's AGENTS.md for that work.

`/etc/os-release` identifies the image version (`IMAGE_VERSION`) and Utah
commit suffix (`BUILD_ID`). Compare with `bootc status` for staged changes.
The bundled `build-source.Containerfile` records default pinned digests;
`gitmodules` and `build-gnome-extensions.sh.txt` record extension origins and
archive checksums. `flavors.json` records the flavor set. Resolve source
commits against the Utah repository in the source map. Never confuse an
extension's release marker with its Git commit.
