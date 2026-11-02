# Disposable ISO review matrix

On a reviewed verification branch, dispatch `build.yml` with `contract_only`
and `review_vm` true. Also set `review_iso_all_flavors=true` to cover every
flavor in `config/flavors.json`; without it, the main flavor runs alone.
The contract job supplies the matrix. Resolve image names with
`scripts/flavors.py image`, never a second hardcoded image list.

The `review_vm` path builds locally only when paired with `contract_only`; it
does not publish or sign an image. If a change modifies the OGC or NVIDIA
kernel-cache inputs, build the matching cache through the normal signed build
pipeline before dispatching the read-only ISO matrix. The review runner must
consume that cache; do not weaken signing or add a signing permission to this
workflow to make the test pass.

Candidate images remain in runner-local storage. The job has only contents
and packages read permissions, disables registry cache writes, and never
publishes or promotes. Record source SHA, flavor, raw payload manifest hash,
ISO checksum, guest bootc status, serial logs and screenshots separately for
each flavor. Fail-fast remains disabled so one failed kernel does not hide
other results. The installed bootc digest must match the offline raw manifest.

The guest flavor probe rejects a different image-info flavor, requires the
running gaming kernel to match `/usr/lib/utah/ogc-kernel-release`, and checks
NVIDIA module vermagic against the running kernel. A QEMU installation proves
these files and kernel boot paths; it cannot prove physical NVIDIA
initialization, suspend, or Secure Boot enrollment. Report those limitations
separately. A passing image build alone does not prove an ISO installation.

The optional installed probe also requires unprivileged user namespaces in the
installed account, plus a loadable TUN device on gaming kernels. Keep terminal
launch stderr in failed fastfetch diagnostics: a user-namespace or sandbox
failure otherwise appears only as blank OCR. An OGC kernel that boots the
desktop but cannot run its declared Flatpaks has failed ISO acceptance.

## Installer art

`iso/scripts/build-iso.sh` and `build-iso-tacklebox.sh` fetch the installer's
Utah mascot (`utah-look-black-outline.png`, used for the installer tour and the
dock icon) from `projectbluefin/artwork` at the commit in `config/artwork.lock`
with `scripts/fetch-artwork.sh`, verify its sha256, stage it as
`iso/live/src/images/utahraptor.png` (git-ignored) and remove it on exit. So an
ISO build needs network for that fetch; `configure-live.sh` fails with a clear
message if the file is missing. Nothing binary is committed for it.

## Bumping bootc-installer

`iso/live/src/install-flatpaks.sh` pins the bootc-installer Flatpak bundle to
a specific `tuna-os/bootc-installer` release rather than resolving
`/releases/latest/download/` the way dakota-iso does: the bundle installs
system-wide with `--no-gpg-verify`, so the version pin is the whole trust
story, and a fixed tag keeps ISO composition reproducible. Its releases are
tagged by build date + commit sha (e.g. `v2026.09.19-cee9ba29`), not semver,
so there is no tag-name continuity to lean on when bumping it.
`UTAH_INSTALLER_VERSION` and `UTAH_INSTALLER_SHA256` must move together —
take the digest from that release's `org.bootcinstaller.Installer.flatpak`
asset (`digest` field of `gh api repos/tuna-os/bootc-installer/releases/tags/<tag>`,
or download and `sha256sum` it) rather than guessing or reusing an old value.
`install-flatpaks.sh` also shims the container-build environment: a machine id,
a session bus (only OCI remotes reach for either), and `/var/tmp` (flatpak's OCI
token cache, hardcoded, ignoring `$TMPDIR`; tmpfiles creates it on boot, but an
image build never boots). All three bite only the OCI `tuna-os` remote.
