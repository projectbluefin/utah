# Image-based installer track (experimental)

The GNOME OS-style install path of
[ADR-0001](../../../adr/0001-image-based-installer.md), run in parallel with
the live ISO + bootc-installer/fisherman path. Nothing gates on it: it is not
a required check, not in `promote-to-testing`'s needs, and it publishes
nothing. Tracking issue: #1403. History: spike #1399.

## What it builds

| Piece | File |
|---|---|
| Live-capable layer on a published digest: gnome-setup and systemd-sysinstall from pinned `hanthor/gnome-setup` and `hanthor/systemd` commits (`GS_REF`, `SYSTEMD_REF`, fetched by commit and compared after checkout), `utah.live` units, initramfs with `systemd-volatile-root` and the live overlay ordered before both root setups (`ostree-prepare-root`, `bootc-root-setup`) | `iso/raw/Containerfile`, `iso/raw/src/` |
| Raw medium: GPT BIOS-BOOT, 512 MiB ESP, 1 GiB ext4 `/boot`, btrfs root; one `bootc install to-filesystem --generic-image --karg rhgb --karg quiet`, with `--composefs-backend` when the image's kernel has the btrfs fs-verity fix (`UTAH_RAW_BACKEND=auto`, `scripts/kernel_verity.py`; ostree otherwise); "Utah Live" BLS entry as GRUB default; `EFI/BOOT` fallback | `iso/raw/build-raw-image.sh`, `iso/raw/medium.py`, `scripts/kernel_verity.py` |
| Offline Flatpaks in the medium's stateful `/var` (`state/os/default/var` on composefs, the stateroot's `var` on ostree), never in an image: the set the ISO bakes (`system-flatpaks.Brewfile` apps via `scripts/verify-desktop-contract.py --flatpaks`, `--no-related`, listed runtimes pinned) plus `flatpak preinstall` of the image's `preinstall.d`, run in the image with that directory bind-mounted, then `setfiles` with the image's contexts | `iso/raw/bake-flatpaks.sh` |
| Shrink to content: fstrim, btrfs balance + resize to minimum (+256 MiB, stepping up while btrfs refuses), fstrim again, cut the root partition and the file, rewrite the backup GPT; `size-breakdown.txt` shows what takes the space | `iso/raw/shrink-raw-image.sh`, `medium.py shrink-plan` |
| End to end with screenshots and `evidence/results.json`; on composefs, fs-verity checks on every installed boot; the fisherman harness's installed-system checks ([below](#installed-system-checks-ported-from-the-fisherman-harness)) | `iso/raw/raw_e2e.py`, `composefs_check.py`, `atspi_drive.py`, `vte_term.py` |
| Size per step (raw, allocated, zstd -3, zstd -19 --long, xz -9, pack/unpack time) | `iso/raw/measure_medium.py` |
| Comparison with the Post-Testing E2E `luks` job for the same digest | `iso/raw/compare_paths.py` |

The installer's post-copy fixups (new filesystem UUIDs and their references,
drop the live entry, `rd.luks.uuid` + crypttab, grow root; deployments under
`ostree/deploy` or composefs's `state/deploy`) are
`iso/raw/src/usr/libexec/utah-sysinstall-post`; the partitions to copy are
written at boot by `utah-live-definitions`. The live session's app is named
"Install Bluefin", as on Dakota's live media; the logo and product name on the
pages come from os-release (`LOGO`, `NAME`). OS-Installer, the first frontend,
was removed once gnome-setup passed every stage nightly (ADR-0001 decision 4).

### gnome-setup (installer=gnome-setup, experimental)

The live layer also carries [gnome-setup](https://gitlab.gnome.org/Teams/STF/setup)
(GNOME STF), built from the [hanthor/gnome-setup](https://github.com/hanthor/gnome-setup)
mirror's `bluefin` branch, shared with Dakota (`GS_REF`), driving `systemd-sysinstall` and
`systemd-cryptenroll` over Varlink. Hummingbird's systemd 261 has neither
Varlink API (v262+), and v262 cannot mount an encrypted root (systemd#44337),
so `systemd-sysinstall`, `systemd-repart` and `systemd-cryptenroll` come from
a pinned [hanthor/systemd](https://github.com/hanthor/systemd) commit
(`SYSTEMD_REF`: systemd main plus `--install-kernel=`/`--install-boot-loader=`),
installed under `/usr/libexec/utah-sysinstall` next to the system's systemd.
The live entry carries `utah.live.installer=gnome-setup` (set by
`build-raw-image.sh`), which starts:

| Piece | File |
|---|---|
| Varlink sockets on the standard paths, live + gnome-setup only; sysinstall with the kernel and boot loader steps off (the copied ESP carries shim and GRUB) | `src/usr/lib/systemd/system/utah-{sysinstall,cryptenroll}.socket`, `@.service` |
| repart.d written at boot: every medium partition but BIOS boot by PARTUUID with `CopyBlocks=`, root into LUKS2 (`key-file+tpm2`, `TPM2PCRs=7` with a TPM 2.0, else `key-file`); gnome-setup's definitions dir links to it | `src/usr/libexec/utah-live-definitions` |
| The post-copy fixups, started by gnome-setup after sysinstall (`[page:installer] post-install-unit=`) and before its recovery key page wipes the empty passphrase | `src/usr/libexec/utah-sysinstall-post`, `utah-sysinstall-post@.service` |
| Pages (language, keyboard, try-or-install, disk, disk passphrase without a TPM, recovery key, done), live user's polkit grants | `src/usr/share/utah/gnome-setup/pages.conf`, `src/usr/share/polkit-1/rules.d/50-utah-live-gnome-setup.rules` |

The recorded encryption policy and the limits of the merged CI evidence
are in [ADR-0001's TPM2 update](../../../adr/0001-image-based-installer.md#update-tpm2-enrollment-and-recovery-key-1405).
PCR 7 unlock is not proof of Secure Boot support: retain the separate
upstream-signing requirement and retest recovery after firmware policy
changes. Recovery-page screenshots contain a working disk key; keep them
confined to disposable test installs, never real user disks.

Keys on the installed root: with a TPM 2.0, the TPM (PCR 7) plus the
recovery key. Without one, gnome-setup's disk passphrase page (shown only
then) enrolls the passphrase the user picks, and the recovery key is a second
slot; the recovery key is never the only boot key.

The E2E (`--tpm`, workflow input `tpm`, default on)
runs the VMs with swtpm: the first boot and the rollback boot must unlock
through the TPM2 with no prompt, and the bootc switch boot runs without the
TPM and is unlocked with the recovery key read from gnome-setup's recovery
key page. Without `--tpm` (`tpm: false`) the harness fills the disk
passphrase page, checks the slots are exactly password + recovery (the
passphrase opens the root, the empty one no longer does), unlocks the first
boot and the rollback boot with the passphrase and the bootc switch boot with
the recovery key. Scheduled and PR CI runs always enable TPM; the TPM-less
path requires a manual workflow dispatch with `tpm: false` or a local run
without `--tpm`. Do not cite a default CI pass as TPM-less coverage.

The GUI harness waits up to 300 seconds for any recognized setup page before
sending Esc to dismiss the overview, settling for three seconds, and maximizing
the installer window. Keep this startup budget separate from the page loop's
shorter unknown-page timeout: an empty accessibility snapshot during slow
application startup must not trigger clicks against fallback window offsets.

`iso/raw/raw_e2e.py` classifies visible gnome-setup pages on each poll,
accepting free-space and plain-confirmation routes. Destructive confirmation
requires `/dev/vdb` exactly; free-space selection never falls back to erase.
Recovery keys come from the key-shaped label; the QR dialog takes precedence
over its parent page. Each distinct page gets one screenshot in the manifest.
Run `python3 -m unittest discover -s tests -p 'test_setup_pages.py'` for
host-only route replays; these do not replace VM runs through both routes.
Unknown pages time out after 60 seconds, interactive pages after 120 seconds,
and progress after an hour, within a 70-minute total budget. Missing controls
save `atspi-gs-miss.json` and log button names and showing/sensitive states.
Failures save `atspi-gs-failure.json` (including hidden nodes), a
`fail-setup-failure` screenshot, the installer log, and the sysinstall journal.
Treat evidence as sensitive: recovery pages and snapshots contain the disk key.

### composefs backend

The medium is built with bootc's composefs backend when the image's kernel
reads btrfs fs-verity data correctly, and with ostree otherwise (see
[Backend selection](#backend-selection)). On composefs: the
repository in `/composefs` (fs-verity on every object), the deployment's
`etc` and `.origin` in `/state/deploy/<verity digest>`, the shared `/var` in
`/state/os/default/var`, Type #1 entries with a `composefs=<digest>` karg and
the kernel in `/boot/bootc_composefs-<digest>/`. The live overlay
(`utah-live-root.service`) runs before `bootc-root-setup.service`, which then
assembles the composefs root from the overlaid `/sysroot`. The repository
itself is bound from the medium back over the overlay: erofs cannot mount
the deployment's image from an overlayfs file ("Block device required"), and
overlayfs does not pass fs-verity through. So the live entry keeps the strict
`composefs=<digest>` karg like the installed ones, and the live root sits at
overlay depth 1. The build fails if
any object lacks verity (`composefs-verity.json`), and so does the shrink
step if compression drops it. Every installed boot of the E2E checks the
strict karg, the `composefs:<digest>` root and verity on every object; the
first boot reads every object end to end and fails on a read error or a
`FILE CORRUPTED` report.

composefs costs space that ostree does not: btrfs keeps each object's
fs-verity Merkle tree (SHA-512, at least one 4 KiB block per object over
4 KiB) in metadata, which is DUP. The no-Flatpak root of run 44910214722 had
514 MB of metadata against 215 MB on ostree (run 44897756832), so about
0.6 GB more on disk, and the hashes do not compress. The full metadata also
makes the shrink step's first resize fail with ENOSPC on composefs; it steps
up and then trims again, or the chunks the resize moved stay allocated in
the file.

The live layer's `RUN` leaves podman's empty, mode 0700 `/etc/resolv.conf`
and `/etc/hostname` mount targets in the image (buildah#4242). ostree's
import deletes them; bootc's composefs backend copied them into the
deployment's `/etc`, so tmpfiles never linked resolved's stub,
NetworkManager wrote its own file into the 0700 one, and resolved logged
"Failed to open /etc/resolv.conf: Permission denied" on every live and
installed boot (run 44923414875). `build-raw-image.sh` removes them when
empty, and the E2E fails a boot that logs it. The live session also skips
`flatpak-preinstall.service`: it would download into the RAM overlay.

bootc labels the `/boot` filesystem root only on its ostree path, and
nothing labels the `/boot/efi` mountpoint or the live entry the build writes
from the (SELinux-less) CI host. On composefs, logind was denied getattr on
the `unlabeled_t` `/boot` root on every installed boot (#1368,
tuna-os/fisherman#504; run 44923414875). After its last write to `/boot`,
`build-raw-image.sh` runs `setfiles` from the image's own policy over the
whole `/boot` filesystem (the ESP is vfat, labeled at mount), records the
labels in `boot-labels.txt` and fails unless the root is `boot_t`. The
install block-copies `/boot`, so the labels carry over, and the E2E fails a
boot with any SELinux denial on a `/boot` path.

The same trap sits under `/var`. `bake-flatpaks.sh` creates
`var/lib/flatpak` from the host, and on a fresh stateroot that `install -d`
creates `var/lib` too, with no label. On first boot systemd (`init_t`) was
then denied `add_name` in the `unlabeled_t` `/var/lib`, and
`systemd-timesyncd` failed its `StateDirectory` until `start-limit-hit`
(Post-Testing E2E run 45858480299, every flavor). The bake runs `setfiles`
over all of `var/lib`, records `/var`, `/var/lib` and `/var/lib/flatpak` in
`var-labels.txt`, and fails unless `/var/lib` is `var_lib_t`. Anything the
medium build writes from the host needs the same treatment.

### Backend selection

Since 7.2, btrfs reads data through large folios, and without mainline
`254e15591ee4` fs-verity then reports intact files as corrupt
(`docs/skills/kernel-cache.md`, "The patched Fedora kernel"). A composefs root
is nothing but fs-verity objects, so `UTAH_RAW_BACKEND=auto` (the default,
and what scheduled and PR runs use) asks `scripts/kernel_verity.py`, run in
the image against its own kernel and rpm database, before `bootc install`.
The kernel counts as fixed if its release carries Utah's patch marker
(`.utah1`: main and nvidia), its package changelog names the fix, it is 7.4
or newer (7.4-rc1 excepted), or it is older than 7.2 without
`CONFIG_BTRFS_EXPERIMENTAL` (the gaming flavors' OGC 7.1.x, whose btrfs only
uses large data folios under that option). A fixed kernel gets composefs, any
other kernel ostree; `composefs` forced on an unfixed kernel fails the build.
The decision is in `raw-evidence/backend.json`, `results.json` (`backend`)
and the job summary. This keeps composefs safe when Utah moves to Fedora's
signed kernel (#1379), which lacks the fix today. The ISO path's composefs
opt-in (`UTAH_E2E_COMPOSEFS_BACKEND=1`, `luks-e2e.sh`) runs the same check on
the live root and fails on an unfixed kernel. Dispatch with `backend=ostree`
for the ostree medium on any kernel.

### Offline Flatpaks

The published image and the live layer stay Flatpak-free; the workflow
fails if either has content in `/var/lib/flatpak`. bootc copies an image's
`/var` only at the first deployment and never touches it again, so Flatpaks
in the medium's stateroot `/var` reach the installed disk through the block
copy and can never be re-added or overwritten by an update. The set is the
ISO's (`iso/live/src/install-flatpaks.sh`) plus the image's `preinstall.d`,
which `flatpak preinstall` records as preinstalled, so
flatpak-preinstall.service has nothing to fetch on first boot and a removed
app stays removed. The E2E proves
both: every installed boot checks the baked list (`flatpak-apps.txt`), and an
app removed after first boot (`--remove-flatpak`, default
`org.gnome.Calculator`) must still be absent after switch and rollback, once
flatpak-preinstall.service has run with the network up.

Each run also measures the same medium without Flatpaks (`size-2-no-flatpaks.json`)
so the comparison shows what the offline set costs in download size.

### Installed-system checks (ported from the fisherman harness)

Everything `iso/scripts/luks-e2e.sh` asserted on the system it installed is
asserted by `raw_e2e.py` too, with the same pass/fail, so the gnome-setup
track can replace it without losing coverage. `Harness.check_installed` runs
every check, records each one's problems under `installed_checks` in
`results.json` and fails the boot with all of them together. It reuses the
fisherman harness's own scripts from their current paths rather than copies;
removing the ISO path must keep them (`tests/test_image_installer.py`
`InstalledSystemCheckTests` fails otherwise):

| Check | How | First boot | Rollback boot |
|---|---|---|---|
| No emergency mode (root is locked, so the boot carries on to `graphical.target`) | `emergency.target`/`emergency.service` started this boot (`emergency_entered`) | yes | |
| `/efi` exists, `efi.automount`/`efi.mount` not failed (#1330) | `EFI_CHECK` | yes | |
| No denied bootc SELinux probe (`comm="chcon"`/`"bootc"`, #1198) | `bootc_probe_denials`; caller diagnostics in `bootc-probe-diag-*.log` | yes | |
| logrotate's `/var/lib` state and service | `LOGROTATE_CHECK` | yes | |
| Flavor: image-info `image-flavor`, OGC kernel booted on gaming (and not elsewhere), `tun` on gaming, NVIDIA module `vermagic` = booted kernel, module file and `nvidia-smi` on nvidia | `iso/scripts/verify-installed-flavor.py` in the guest | yes | |
| Unprivileged user namespaces (Flatpak sandboxes), `gnome-rounded-blur` installed and `-devel` absent | `unshare` as the user; `rounded_blur_problems` | yes | |
| CountMe: timer enabled and active, a mocked report names `utah/<flavor>:`, the opt-out marker stops the service | `iso/scripts/verify-countme-runtime.sh` in the guest | yes | |
| GNOME extensions (`EXTENSIONS`, gsconnect) reach ACTIVE within two minutes | `iso/scripts/extension-state.sh` | yes | |
| fastfetch in a terminal, read back by tesseract, and that frame differs from the clean desktop's. The shell left open afterwards is `bash --norc`: Bluefin's uwelcome banner otherwise scrolls fastfetch's kernel/desktop/shell lines out of the window and the OCR gate fails on a correct desktop (run 45858480299) | `vte_term.py` + `iso/scripts/fastfetch-ocr-match.sh` | yes | |
| System Flathub remote configured | `flathub_configured` | yes | yes |
| Setup units ran, every `[services]` unit of the installed contract still enabled and not failed | `iso/scripts/e2e-services.sh` `verify_setup_state` + `scripts/verify-desktop-contract.py`, in the guest as the user | yes | yes |

The flavor is `--flavor`, or the configured flavor that publishes
`--candidate`'s repository (`scripts/flavors.py flavor IMAGE`): the switch
target is always the same repository as the image under test. The rollback
boot is the fisherman harness's repeat boot (#19). The bootc switch boot runs
another image and gets none of these checks: an older `:testing` cannot meet
rules this checkout added later (#1343). The live session, desktop and
greeter screenshots must draw (retaken for a minute, then the run fails), the
live one below the top bar too, as `UTAH_E2E_REQUIRE_SCREENSHOTS` required.
The harness needs `tesseract` on the host.

Not ported, because they test fisherman's path rather than the installed
system: the ISO's `media-input.json` receipt and the booted manifest/config
tie to it (the medium is a derived live layer installed by block copy, so its
digest is not the published one; `bootc switch` and `bootc rollback` check
digests instead), fisherman's lost-Flatpak warning, the boot-entry `rhgb`
patch, the live `btrfs` modprobe, and the `docs/verification` record.
Unlike the fisherman VM, these VMs have network: the CountMe check still
stubs curl, but the shipped timer can reach the real endpoint at boot.

## Running it

`.github/workflows/image-installer.yml` runs nightly (07:41 UTC, after the
testing build and Post-Testing E2E), on `workflow_dispatch`, and on pull
requests that touch `iso/raw/**`, the workflow, or
`tests/test_image_installer.py`. Dispatch inputs: `image_ref` (exact
`ghcr.io/projectbluefin/<image>@sha256:...`), `all_flavors`, `install`
(`gui` drives gnome-setup's window through a QEMU USB tablet at AT-SPI
extents; `script` runs the same sysinstall call over SSH), `backend`
(`auto`, the default and what scheduled and PR runs use; `composefs`, which
fails on an unfixed kernel; or `ostree`),
`btrfs_compress`
(default `zstd:3`, also on scheduled and PR runs; `off` turns it off, and an
empty value means the default, because GitHub substitutes the default for an
empty dispatch input: the
shrink step rewrites every file on the root compressed after the Flatpak
bake; mounting with `compress-force` alone does nothing, because ostree and
flatpak fallocate their objects), `flatpaks` (default on: bake
the set above into the medium's `/var`; off leaves the image's `preinstall.d`
to the online first-boot flatpak-preinstall.service), `formats`.

The job summary holds the comparison table; the
`image-installer-<image>` artifact holds screenshots, `contact-sheet.png`,
evidence, raw-evidence and `comparison.json`. The medium and the disks are
never uploaded: CI media carry a root SSH key, `utah.live.debug` (live user
password `live`, sshd) and the test passphrase.

Locally, with a published digest and root (KVM, podman, qemu, OVMF, zstd, xz;
the E2E also needs tesseract and ffmpeg).
The build takes about 10-15 minutes; `bake-flatpaks.sh` is optional (about
5 GB more raw, needs Flathub; without it the installed system preinstalls
them online on first boot):

```bash
sudo podman pull ghcr.io/projectbluefin/utah:testing
sudo podman build --layers=false -t localhost/utah-raw:local \
  --build-arg SOURCE_IMAGE=ghcr.io/projectbluefin/utah:testing \
  -f iso/raw/Containerfile iso/raw
sudo bash iso/raw/build-raw-image.sh localhost/utah-raw:local \
  output/medium.raw ghcr.io/projectbluefin/utah:testing
sudo bash iso/raw/bake-flatpaks.sh localhost/utah-raw:local output/medium.raw   # optional
sudo env UTAH_RAW_BTRFS_COMPRESS=zstd:3 \
  bash iso/raw/shrink-raw-image.sh localhost/utah-raw:local output/medium.raw
sudo chown "$USER" output/medium.raw
```

`dd` the shrunk `medium.raw` to a USB stick (UEFI only; no El Torito yet,
#1404). Without `UTAH_RAW_LIVE_KARGS=utah.live.debug` the live user has no
password and there is no sshd.

### Try it in a VM

Boot the medium read-only next to a blank target disk of at least 48 GB
(the medium's partitions must fit). This needs a graphical session for
`-display gtk`; use `-display vnc=:1` over SSH. The firmware paths are
Homebrew's QEMU (`brew install qemu`, the usual one on Bluefin/Utah hosts);
on Fedora use `/usr/share/edk2/ovmf/OVMF_CODE.fd` and `OVMF_VARS.fd`:

```bash
cd output
Q=/home/linuxbrew/.linuxbrew/share/qemu
cp $Q/edk2-i386-vars.fd vars.fd
qemu-img create -f qcow2 target.qcow2 64G
qemu-system-x86_64 -enable-kvm -machine q35 -cpu host -smp 4 -m 8192 \
  -drive if=pflash,format=raw,readonly=on,file=$Q/edk2-x86_64-code.fd \
  -drive if=pflash,format=raw,file=vars.fd \
  -drive file=medium.raw,format=raw,if=virtio,readonly=on \
  -drive file=target.qcow2,format=qcow2,if=virtio \
  -device virtio-vga-gl -display gtk,gl=on \
  -nic user,model=virtio-net-pci
```

1. GRUB boots "Utah Live (try or install)" by default; the live session
   autologs in and gnome-setup starts.
2. Pick the 64 GB disk, install, back up the recovery key. "Installation Complete!"
   appears in about one to two minutes.
3. Power off, then run the same command without the `medium.raw` line. The
   installed disk boots through the "Utah" firmware entry the installer
   wrote (no GRUB menu): branded passphrase prompt, gnome-initial-setup,
   desktop. `bootc status` shows it tracking `ghcr.io/projectbluefin/utah:testing`;
   `sudo bootc upgrade`, `bootc switch` and `bootc rollback` work as on any
   installed Utah.

Keep `vars.fd` between boots: the installer's firmware entry lives there.
Recreate `target.qcow2` (and copy a fresh `vars.fd`) to install again.

## What the comparison measures

| Metric | Image-based | ISO/fisherman (`luks` job, same digest) |
|---|---|---|
| Medium | raw and allocated bytes after bootc install and after shrinking; compressed bytes and pack/unpack time per format | `ISO ready` line; `production-iso-<image>` artifact bytes |
| Build time | image layer + `bootc install` | "Compose production ISO" step |
| Install time | GUI confirm-to-done | `=== Phase 3/7` to `=== Phase 4/7` |
| Stages | live boot, install, first boot unlock, initial setup, first boot desktop, bootc switch, bootc rollback | phases 1-7 (failed phase from the last marker) |
| SELinux, failed units | `getenforce`, `systemctl --failed` on every boot | live permissive by construction (#22); not recorded |

GNOME OS's published ISOs (nightly, stable) are read with HEAD requests as a
size reference.

## Gotchas found on the way

- `systemd.volatile=overlay` disables ostree's generated `var.mount`
  (`ConditionKernelCommandLine=!systemd.volatile`), so `/var` stays read-only
  and D-Bus fails. `utah-live-root.service` runs `systemd-volatile-root
  overlay /sysroot` behind the Utah-only karg `utah.live` instead.
- dracut in a container build needs `--tmpdir /tmp`, and Utah's initramfs
  lacks `systemd-volatile-root` until `60-utah-live.conf` adds it.
- `bootc install to-disk --filesystem btrfs` puts `/boot` inside the root;
  GRUB cannot unlock repart's LUKS2 (argon2id), so the medium is partitioned
  by hand with a separate ext4 `/boot`.
- Without `rhgb quiet` on the deployment the installed disk asks for the
  passphrase in Plymouth's text mode; `bootc install` and `kargs.d` set
  neither, fisherman adds them (`desktop-contract/references/boot-splash.md`).
- GNOME Shell does not focus a window that maps under the login overview,
  and GTK 4 has no AT-SPI `GrabFocus` or actions on list rows: the harness
  clicks with a USB tablet at AT-SPI extents. GTK 4 reports extents relative
  to the window, so the window is maximized first (it then starts under the
  32 px top bar).
- The pinned-fetches gate scans workflow text: say "bootc switch" only in
  comments, and fetch git sources by commit (`git fetch <url> <sha>`), not
  `git clone`.
