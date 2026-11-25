# ADR-0001: Image-based installer (GNOME OS-style), in parallel with fisherman

- **Status:** Proposed
- **Date:** 2026-11-21
- **Tracking:** #1403 (track and comparison runs), epic #1236 (Dakota parity)
- **Follow-ups:** #1404 (hybrid ISO, systemd-boot/UKIs), #1405 (TPM2, recovery
  key), #1406 (Dakota branches), #1379 (btrfs fs-verity fix in a signed
  kernel), #1435 (gnome-setup + systemd-sysinstall as the installer frontend)
- **History:** feasibility spike #1399 (not merged; superseded by the track);
  composefs medium #1423 ([composefs backend](#update-composefs-backend-1423))

## Context

### How Utah installs today

Utah ships a live ISO (`iso/scripts/build-iso.sh`, `iso/live/`): a
dmsquash-live squashfs with an offline containers-storage copy of the image
under `/usr/lib/containers/storage`, the default Flatpaks baked in, and the
[bootc-installer](https://github.com/tuna-os/bootc-installer) Flatpak
(pinned bundle) driving fisherman. fisherman partitions the disk, sets up LUKS
with `cryptsetup`, then runs `bootc install to-filesystem` **on the user's
machine**, from that offline store, and creates the user. Post-Testing E2E's
`luks` job (`iso/scripts/luks-e2e.sh`) installs every flavor this way and
gates `:testing` promotion.

### Pain points, with evidence

1. **Install is a full `bootc install` at install time, and slow.** Phase 3 of
   the last green `luks` job for the current `:testing` digest took 7 min 55 s
   (run [44673956304](https://github.com/projectbluefin/utah/actions/runs/44673956304),
   job 136081573673, `=== Phase 3/7` at 04:36:48 to `=== Phase 4/7` at
   04:44:43). dakota-iso documents 5-15 min (`docs/luks-testing.md:250`).
2. **It fails in components Utah does not own, after minutes of work.** In
   run [44868527178](https://github.com/projectbluefin/utah/actions/runs/44868527178)
   every flavor's `luks` job failed in phase 3: fisherman reached bootupd after
   550 s, then `cp: File "/tmp/.tmpDFFusw/EFI/fedora/grubx64.efi": Operation
   not supported (os error 95)` and `bootc_install_failed` (job 136738020861).
   Earlier install-time breakages: #1129 (composefs installs: podman staging in
   RAM, `chpasswd --root` running PAM, fisherman#345/#396/#484), #602
   (`useradd` and a dangling `/home`), #136 (live kernel without btrfs).
3. **The live session is SELinux-permissive.** The squashfs is assembled
   rootless and unlabeled, so `build-iso.sh` boots it with `enforcing=0`
   (documented exception #22, every DEBUG value; #186).
4. **The medium is a second OS assembly with a size budget to defend.** The
   squashfs holds the live root plus an offline containers-storage store that
   only stays cheap while mksquashfs can deduplicate it against the live root
   (an OCI layout was a second copy: 9.4-11.3 GiB per flavor, PR #1131); see
   `build-iso.sh:13-29`, #684, #128. The `utah` ISO is 5.3 GB (`ISO ready:
   ... (5.3G)`, run 44673956304; the `production-iso-utah` artifact is
   5,556,307,570 bytes) against an 8 GiB guard.
5. **The machine a user gets is assembled on their hardware.** CI images,
   the live session and the installed disk are produced by three different
   steps (image build, squashfs assembly, install-time `bootc install`), so a
   passing image build does not prove an installable system.

### What GNOME OS does instead

GNOME OS (gnome-build-meta) ships one image, boots it live with one karg
(`root=live:gnomeos`, carried in a UKI addon only the medium has), and its
proto-installer daemon installs by block copy (`systemd-repart`
`CopyBlocks=auto`, encrypt-on-copy, TPM2 enrolled by repart). It does **not**
use os-installer and does not `dd` a finished image: `/usr` is a dm-verity
partition and the live root is migrated onto the target. The principle ("one
image, a live karg, install = copy, the karg does not follow") carries over to
bootc; the mechanism has to change because a bootc root *is* the OS.

## Decision

Build, in parallel with the ISO/fisherman path and without changing it, an
**image-based install path**:

1. **The medium is a bootc raw disk image.** It is built once in CI with
   `bootc install to-filesystem --generic-image --karg rhgb --karg quiet
   --target-imgref ghcr.io/projectbluefin/utah:<stream>` into GPT with an
   ESP, a separate ext4 `/boot` (GRUB cannot unlock repart's LUKS2/argon2id)
   and a btrfs root (`iso/raw/build-raw-image.sh`). It uses bootc's
   composefs backend where the image's kernel has the btrfs fs-verity fix,
   and ostree otherwise (`backend=auto`, see
   [composefs backend](#update-composefs-backend-1423)).
2. **It boots live with one karg, `utah.live`.** A "Utah Live" BLS entry is
   the medium's default. In the initrd, `utah-live-root.service` runs
   `systemd-volatile-root overlay /sysroot` before `ostree-prepare-root`
   (ostree) or `bootc-root-setup` (composefs), so the live session writes to
   RAM and the medium stays byte-for-byte what is installed. `!utah.live`
   drop-ins keep updaters, `/boot` writers and the Flatpak preinstall off.
3. **Install is a block copy into LUKS2**, done today by
   [OS-Installer](https://gitlab.gnome.org/p3732/os-installer) with Utah's
   config and `install.sh` from
   [hanthor/os-installer](https://github.com/hanthor/os-installer)
   (`utah_config/`, pinned by commit): mirror the medium's GPT into
   `repart.d` with `CopyBlocks=`, `Encrypt=key-file` on root, regenerate every
   filesystem UUID and rewrite its references, drop the live entry, add
   `rd.luks.uuid` and crypttab. gnome-initial-setup creates the user on first
   boot.
4. **The installer frontend is gnome-setup driving systemd-sysinstall**
   (owner decision, 2026-11-21; #1435). GNOME's
   [gnome-setup](https://gitlab.gnome.org/Teams/STF/setup) replaces
   os-installer, and systemd-sysinstall runs the install from this medium's
   repart definitions (`CopyBlocks=` of the medium's partitions, or seed +
   migrate). The earlier plan to port to a home-grown proto-installer-style
   daemon is dropped. os-installer stays in the track only as the working
   fallback, behind an input, until gnome-setup passes the seven-stage E2E;
   do not grow its shell contract beyond what the comparison needs.
5. **bootc remains the updater.** The copied deployment keeps its origin;
   `bootc upgrade`/`switch`/`rollback` work as on a fisherman install. No
   systemd-sysupdate A/B partitions.
6. **Offline Flatpaks live on the installer medium, never in the image.** The
   published `ghcr.io/projectbluefin/utah` image stays Flatpak-free: no
   Containerfile change and no image layer carries Flatpaks. After `bootc
   install to-filesystem` has written the deployment, `build-raw-image.sh`
   installs the ISO's Flatpak list into the medium's stateful `/var` (the
   stateroot's `var/lib/flatpak` on the raw image's root filesystem), the same
   place the ISO bakes them outside the OCI image. The block copy carries
   that `/var` to the installed disk, so first boot starts with them and needs
   no network.
7. **Constraints set by the owner:** Secure Boot through the upstream
   shim/GRUB (later systemd-boot) chain only, no Utah key; Flatpak-first;
   Dakota is the parity target, and this path may replace bootc-installer for
   Dakota too (#1406).

The track lives in `iso/raw/` and `.github/workflows/image-installer.yml`
(nightly and on dispatch against the current `:testing` digest). It is
experimental: not in `promote-to-testing`'s needs, not a required check, and
it changes no existing job.

## Consequences

- **Positive.** Install is a copy (minutes, no `bootc install` and no
  network on the user's machine); the live session runs SELinux enforcing
  because `bootc install` labels the image; the medium is the installed
  system itself, with no separate live root or payload store to keep in step;
  what CI boots is what the user gets, byte for byte; the same `install.sh`
  can serve Dakota with layout branches. ostree/bootc never touch `/var`
  after the first deployment, so a bootc update cannot re-add or overwrite
  the baked Flatpaks, and an app the user removes stays removed (CI asserts
  both: the OCI image has no `/var/lib/flatpak` content, and a removed app
  is still absent after bootc switch and rollback).
- **Negative.** Two install paths to maintain while the comparison runs. The
  medium is a USB/virtual-disk image until #1404. os-installer is a personal
  upstream with known rough edges (no privilege model, no TPM2 or
  recovery-key page, the encryption page's Continue check tests truthiness,
  not validity). On an ostree medium the live overlay puts the root at
  overlay depth 2, the kernel maximum, so nested overlay users such as
  podman's overlay driver are not expected to work in the live session
  (untested); a composefs medium binds its repository past the overlay and
  sits at depth 1.
- **Neutral.** gnome-initial-setup moves from "live session" (GNOME OS) to
  "first boot", which Utah already does (`images.json`:
  `needs_user_creation: false`). The baked Flatpaks are a snapshot of the
  medium's build day; Flatpak updates them afterwards, as on a fisherman
  install.

## What was proven

Spike #1399, all stages green in both the `gui` (real OS-Installer window,
clicked through a QEMU USB tablet at AT-SPI extents) and `script` jobs:
[44866213203](https://github.com/hanthor/utah/actions/runs/44866213203) and
again on the PR head
[44868741985](https://github.com/hanthor/utah/actions/runs/44868741985).
Screenshots:
[evidence branch, run 44866213203 gui](https://github.com/hanthor/utah/tree/spike/gnomeos-installer-evidence/run-44866213203-gui).

| Stage | Screenshot | Result |
|---|---|---|
| GRUB boots "Utah Live (try or install)" from the raw medium | `00-grub-menu.png` | pass |
| Live session, read-only disk, tmpfs overlay, **SELinux enforcing**, OS-Installer autostarted | `01-live-session.png` | pass |
| Installer pages: language, welcome, keyboard, disk, encryption, confirm, summary | `02`...`08-installer-*.png` | pass |
| Install complete 145 s after Confirm (`install.sh` 144 s, 138 s of it the repart copy) | `09-installer-progress.png`, `10-installer-done.png`, `11-installer-log.png` | pass |
| Installed disk alone: branded graphical Plymouth LUKS prompt | `12-first-boot-luks-prompt.png` | pass |
| gnome-initial-setup creates the user | `13-gis-00`...`06-*.png` | pass |
| Desktop and `bootc status` tracking `ghcr.io/projectbluefin/utah:testing` | `14-first-boot-desktop.png`, `14-first-boot-bootc-status.png` | pass |
| bootc switch to the next `:testing` digest, reboot, unlock, login | `20`...`22-switch-*.png` | pass |
| bootc rollback, reboot, unlock, login, installed digest again | `30`...`32-rollback-*.png` | pass |

SELinux was Enforcing and `systemctl --failed` empty on every installed boot;
`btrfstune -u` regenerated the root's fsid while the medium's identical fsid
was mounted. Raw image: 7.1 GiB allocated, 3.15 GiB at zstd -3, built by one
`bootc install to-filesystem` in 147 s; this medium did not yet carry the
offline Flatpaks.

## Update: composefs backend (#1423)

### Decision

The medium is built with `bootc install to-filesystem --composefs-backend`
by default: the composefs repository in `/composefs` with fs-verity on every
object, the deployment in `/state/deploy/<verity digest>`, Type #1 entries
with a strict `composefs=<digest>` karg. The choice is `backend=auto`
(`UTAH_RAW_BACKEND`, `image-installer.yml`'s `backend` input), decided inside
the image by `scripts/kernel_verity.py` and recorded in
`raw-evidence/backend.json`: composefs where the image's kernel has the
btrfs fs-verity fix, ostree on any other kernel, and a forced `composefs` on
an unfixed kernel fails the build. ostree stays a dispatch option
(`backend=ostree`) and the fallback.

### The kernel dependency

Since 7.2, btrfs reads data through large folios, and without mainline
`254e15591ee4` ("btrfs: fix the fsverity callback where unexpected range is
verified", first in v7.4-rc2, no `Cc: stable`) fs-verity reports intact
files as `FILE CORRUPTED` and sometimes returns `EIO`. A composefs root is
nothing but fs-verity objects. Utah's kernel is Fedora's rebuilt with the
patch (`7.3.4-200.1.utah1.fc44`, `docs/skills/kernel-cache.md`); the
Fedora-signed kernel `os-minimal` ships lacks it. The
upstream route is the kernel-ark `fedora-7.3` backport,
[kernel-ark!4899](https://gitlab.com/cki-project/kernel-ark/-/merge_requests/4899),
tracked in #1379. Moving Utah to a signed kernel without the fix therefore
moves the medium back to ostree instead of breaking it; the selector accepts
the `.utah` marker, the fix in the package changelog, 7.4 or newer, and
kernels older than 7.2 without `CONFIG_BTRFS_EXPERIMENTAL` (the gaming
flavors' OGC 7.1.x).

### What was proven

All seven stages passed on composefs with the ISO's Flatpak set baked and the
root compressed with zstd:3, on `utah@sha256:1c7956e2…`:
[44923414875](https://github.com/projectbluefin/utah/actions/runs/44923414875)
and on #1423's merged head
[44929669999](https://github.com/projectbluefin/utah/actions/runs/44929669999).
SELinux was Enforcing and no unit failed on any boot, and on the merged head
no boot logged an SELinux denial at all. Every object kept fs-verity through
the block copy and every boot: 88,325 of 88,325 on first boot, where all
6.38 GB were read back without an error or a `FILE CORRUPTED` report, and
98,153 of 98,153 after `bootc switch` and after rollback. `install.sh` took
59-70 s, against 44-56 s on ostree.

### Size: the composefs metadata cost

btrfs stores each object's fs-verity Merkle tree (SHA-512) in metadata,
which is DUP. The root's metadata grew from 215 MB on ostree to 514 MB on
composefs, about 0.3 GB more written twice, and hashes do not compress, so it
costs on disk and in the download. The full metadata also made the shrink
step's first resize fail with ENOSPC; its retries moved chunks and left
about 0.9 GB stale in the file until #1423 trimmed after the resize.

### Size: compression trade-off

Compressing the medium's btrfs root (`btrfs_compress`, `defragment -c` after
the Flatpak bake, zstd:3 by default) makes the medium smaller on disk and as
a raw download, but the zstd download larger: zstd cannot recompress what
btrfs already compressed in 128 KiB extents as well as it compresses the
plain data in one stream. On ostree without Flatpaks:

| ostree, no Flatpaks | Raw | Allocated | zstd -3 | zstd -19 --long |
|---|---|---|---|---|
| uncompressed root ([44885052941](https://github.com/projectbluefin/utah/actions/runs/44885052941)) | 9.47 GB | 7.33 GB | **3.37 GB** | **2.40 GB** |
| zstd:3 root ([44905911134](https://github.com/projectbluefin/utah/actions/runs/44905911134)) | 7.06 GB | **5.16 GB** | 4.53 GB | 3.63 GB |

So: compressed for a medium written to a USB stick or a disk as is,
uncompressed for the smallest download. The ISO it would replace is 5.56 GB.

### Smallest passing configurations

Every row passed all seven stages on `utah@sha256:1c7956e2…`; composefs rows
kept fs-verity on every object on every installed boot. Sizes in GB (10^9).

| Medium | Run | Raw | Allocated | zstd -3 | zstd -19 --long | `install.sh` |
|---|---|---|---|---|---|---|
| ostree, no Flatpaks, uncompressed | [44885052941](https://github.com/projectbluefin/utah/actions/runs/44885052941) | 9.47 | 7.33 | 3.37 | **2.40** | 56 s |
| ostree, no Flatpaks, zstd:3 | [44905911134](https://github.com/projectbluefin/utah/actions/runs/44905911134) | 7.06 | **5.16** | 4.53 | 3.63 | 44 s |
| composefs, no Flatpaks, zstd:3 | [44934613515](https://github.com/projectbluefin/utah/actions/runs/44934613515) | 7.59 | **4.98** | 3.92 | 3.14 | 51 s |
| ostree, Flatpaks, zstd:3 | [44897756832](https://github.com/projectbluefin/utah/actions/runs/44897756832) | 8.94 | **6.40** | 5.56 | **4.72** | 54 s |
| composefs, Flatpaks, zstd:3 | [44938602008](https://github.com/projectbluefin/utah/actions/runs/44938602008) | 10.01 | 7.04 | 5.79 | 5.05 | 86 s |

Without Flatpaks the smallest download is the uncompressed ostree medium
(2.40 GB at zstd -19 --long) and the smallest disk footprint the zstd:3
composefs medium (4.98 GB allocated). With the ISO's Flatpak set the smallest
passing medium is ostree with zstd:3 (6.40 GB allocated, 4.72 GB at zstd -19
--long); composefs costs 0.3-0.6 GB more for the same content. No
uncompressed composefs medium, and no uncompressed Flatpak medium on the
256 MiB-chunk shrink, has been tested: `image-installer.yml` documents an
empty `btrfs_compress` as "off", but GitHub substitutes the `zstd:3` default
for an empty dispatch input, so dispatches for them ran zstd:3 instead
(44938602008, 44944477347). The 1 GiB-chunk uncompressed Flatpak medium,
run [44890374158](https://github.com/projectbluefin/utah/actions/runs/44890374158),
was 13.10 GB allocated and 3.87 GB at zstd -19 --long. The ISO is 5.56 GB.

### Findings fixed on the way

- **`/etc/resolv.conf`.** A podman/buildah `RUN` leaves empty, mode 0700
  `/etc/resolv.conf` and `/etc/hostname` in the image (buildah#4242).
  ostree's import deletes them; bootc's composefs backend copied them into
  the deployment's `/etc`, so tmpfiles never linked resolved's stub and
  resolved logged "Failed to open /etc/resolv.conf: Permission denied" on
  every live and installed boot. The build removes them when empty, and the
  E2E fails a boot that logs it.
- **Unlabeled `/boot`.** bootc labels the `/boot` filesystem root only on its
  ostree path, and nothing labeled the `/boot/efi` mountpoint, so logind was
  denied getattr on `/boot` (`unlabeled_t`) on every installed composefs boot
  (#1368, tuna-os/fisherman#504). We build the medium, so the build labels
  all of `/boot` from the image's own policy after its last write there; the
  block copy carries the labels, and the E2E fails any boot with a denial on
  `/boot`.
- **Flatpak preinstall in the live session.** `flatpak-preinstall.service`
  would download every Flatpak into the RAM overlay of a medium without
  baked Flatpaks; it is skipped under `utah.live`.

## Update: TPM2 enrollment and recovery key (#1405)

### Recorded policy

The gnome-setup implementation merged in #1437 supplies the encryption
policy for this experimental track; this records that existing behavior,
not a change to the ISO/fisherman installer or its defaults.

- **Encrypt root on every install.** With a TPM 2.0, systemd-repart uses
  `Encrypt=key-file+tpm2` and enrolls the TPM during the copy. Without one,
  it uses `Encrypt=key-file`, and gnome-setup asks for a disk passphrase.
  TPM enrollment is automatic when the hardware is present, not a separate
  opt-in page. The workflow defaults to `installer=gnome-setup`, `tpm=true`.
- **Always enroll a generated recovery key.** gnome-setup presents the
  systemd-cryptenroll recovery key and QR code on its recovery-key page.
  The user must save it outside the installed disk. After post-copy fixups,
  enrollment removes the temporary empty passphrase: the final keys are
  TPM2 + recovery with a TPM, or the user's passphrase + recovery without
  one. A recovery key must never be the only boot key on a TPM-less machine.
- **Bind the TPM to PCR 7, not pcrlock.** The current repart definitions
  set `TPM2PCRs=7`. This is the implemented policy for the existing
  shim/GRUB chain; it does not bind unlock to each bootc deployment's kernel
  or root digest. Changes to the firmware's Secure Boot policy can require
  the recovery key. Revisit the policy with the systemd-boot/UKI work
  (#1404), rather than silently introducing pcrlock here.
- **Do not equate TPM unlock with Secure Boot support.** PCR 7 enrollment
  alone does not make an unsigned kernel trusted or protect every part of
  the boot chain. Utah still needs the upstream signing work tracked in
  #1379; no Utah signing key or MOK enrollment is introduced by this track.

The definitions and post-copy glue live in
`iso/raw/src/usr/libexec/utah-live-definitions` and `utah-sysinstall-post`;
key presentation and enrollment belong to gnome-setup, not a new
proto-installer daemon. The selectable os-installer fallback retains its
passphrase-only contract; do not extend it with a second encryption UI.

### Evidence and limits

Merged #1437 records CI run
[44990062805](https://github.com/projectbluefin/utah/actions/runs/44990062805)
on `5c8f59a`: a composefs medium with baked Flatpaks passes GUI installation,
unattended TPM2 unlock on first boot, recovery-key unlock without the TPM
after `bootc switch`, and unattended TPM2 unlock after rollback. The
workflow runs an emulated TPM (`swtpm`), retaining its state across TPM boots.
The same PR records local ostree and composefs runs without baked Flatpaks;
it does not establish ostree-with-Flatpaks CI coverage, consecutive nightly
passes, real-hardware firmware behavior or Secure Boot support.

The TPM-less E2E harness separately checks password + recovery slots,
rejects the empty passphrase, boots with the passphrase, uses recovery after
switch, and returns to the passphrase after rollback. This path is outside
default CI: scheduled and PR runs enable the emulated TPM; a manual workflow
dispatch with `tpm: false` (or a local run without `--tpm`) selects the
TPM-less path. The successful TPM-enabled run above is not evidence that
this separate path ran in default CI. See the
[image-installer testing reference](../skills/local-testing/references/image-installer.md#gnome-setup-installergnome-setup-experimental)
for the harness contract. These are disposable CI keys: the recovery page
is captured in evidence, and CI disks/media must never be used for a real
installation or uploaded as distributable installer images.

## What remains before it can replace fisherman

1. **Medium format** (#1404): an El Torito hybrid (`systemd-repart
   --el-torito`) with the kernel reachable from the ESP, which means
   systemd-boot and UKIs; the live karg then becomes a medium-only UKI addon.
2. **Offline Flatpaks on the medium** (decision item 6): done in #1413,
   installed into the medium's stateful `/var` at build time from the list
   the ISO bakes, with the image itself kept Flatpak-free.
3. **Encryption UX** (#1405): policy and implementation recorded
   [above](#update-tpm2-enrollment-and-recovery-key-1405), with TPM2 and
   recovery-key switch/rollback evidence from #1437. Hardware validation
   and any policy change for systemd-boot/UKIs remain separate work.
4. **Medium layout and `rhgb quiet` in the real build**: today they are set by
   `iso/raw/build-raw-image.sh`, held by tests; they move into the published
   build when the path is adopted.
5. **Dakota branches** (#1406): ESP-resident Type #1 entries, `loader.conf`,
   `LABEL=root`. The composefs `state/deploy` layout and the live overlay
   before `bootc-root-setup` are done on Utah's composefs medium (#1423).
6. **Installer frontend** (decision item 4, #1435): gnome-setup +
   systemd-sysinstall on the track, beside os-installer, until it passes the
   seven stages. Open: sysinstall's bootloader step assumes systemd-boot and
   a UKI `kernelImagePath` (fits Dakota; Utah boots shim+GRUB),
   `BlockDeviceReplace=` refuses a seed filesystem, the composefs/bootc
   state (origin, `state/deploy`, fs-verity) after install, who does the
   post-copy fix-ups, Utah's missing Varlink socket, and packaging
   gnome-setup.
7. **composefs on a signed kernel** (#1379): today only Utah's rebuilt
   kernel carries the btrfs fs-verity fix, so `backend=auto` falls back to
   ostree on any flavor or base without it.

## Alternatives considered

- **Keep fisherman/bootc-installer as is.** Known and gating today. Keeps
  every pain point above: install-time `bootc install` (minutes, failure
  surface outside Utah), permissive live session, payload twice on the
  medium. Remains the shipped path until the exit criteria below are met.
- **GNOME OS's proto-installer as is.** In gnome-build-meta (master
  `143bdf7`, also gnome-49/50/51) it is `files/gnomeos/live/proto-installer`,
  built into the OS image by `elements/gnomeos/live.bst` and gated on
  `root=live:gnomeos`: a Python/libadwaita UI (333 lines) and a Rust zbus
  daemon (`org.gnome.Installer1`, polkit) that writes `repart.d` (ESP; usr +
  verity slot A with `CopyBlocks=auto`; empty B slots; root with
  `Encrypt=key-file+tpm2` and `BlockDeviceReplace=/`, unencrypted without a
  TPM), runs systemd-repart and swaps verity, with a recovery key, pcrlock
  and OEM mode. The install logic is GNOME OS specific and it has no
  passphrase, language or keyboard pages. os-installer appears nowhere in
  gnome-build-meta. A daemon modelled on it was the earlier plan for decision
  item 4; dropped for gnome-setup + systemd-sysinstall.
- **gnome-setup + systemd-sysinstall** (chosen, decision item 4, #1435).
  [gnome-setup](https://gitlab.gnome.org/Teams/STF/setup) is GNOME's
  STF-funded successor (Rust/GTK4/blueprint, active through 2026-09-29;
  not yet packaged by gnome-build-meta): an OEM-style installer meant to
  become the initial-setup wizard too (language, keyboard, prerelease, EULA,
  try or install, disk select, wipe or free space, confirm, recovery key
  with QR through systemd-cryptenroll, done; then gnome-initial-setup). It
  drives systemd-sysinstall (systemd v261+) over Varlink
  (`io.systemd.SysInstall.Run(node, definitions, erase, kernelImagePath,
  copyLocale/Keymap/Timezone)`, progress `ValidateBlockDevice` …
  `InstallBootloader`; `ListCandidateDevices`), with distribution-provided
  `repart.d` definitions. Our definitions would be `CopyBlocks=` of the
  medium's partitions, or the seed + migrate mode below. It replaces both
  os-installer and a home-grown daemon. Utah (`utah@sha256:1c7956e2…`,
  systemd 261.2) ships `/usr/bin/systemd-sysinstall` and
  `systemd-sysinstall.service` but no `.socket` or `@.service`, so the
  Varlink service is not socket-activatable on Utah today; Dakota ships all
  four. Open questions are listed under "What remains", item 6.
- **systemd-sysupdate A/B instead of bootc.** Would replace the updater, the
  `:testing`/`:stable` streams, the promotion and release gates and uupd. The
  spike proved bootc upgrades and rolls back a copied disk, so the updater
  does not need to change for this install path.
- **Single btrfs metadata on the medium, DUP at install** (rejected,
  #1423). Writing the medium's metadata once (`mkfs.btrfs -m single`) and
  converting it back to DUP at install (`btrfs balance start
  -mconvert=dup` in `install.sh`) would save about 0.3-0.6 GB of composefs
  fs-verity metadata on disk and in the download. It needs a change to
  hanthor/os-installer's `install.sh` and leaves the medium without metadata
  redundancy. Rejected: redundancy is worth more than 0.3-0.6 GB.
- **Seed + migrate (no-reboot) install mode**, see
  [dakota#1920](https://github.com/projectbluefin/dakota/pull/1920) and the
  [spike report](https://gist.github.com/ahmedadan/48b7687dffde49336f78149f8d054b70).
  Open, not adopted. The live root is a btrfs seed device made writable by
  a zram device; install adds the LUKS2 (TPM2, PCR 7) target to the running
  filesystem and removes the zram device and the seed, so btrfs migrates the
  session onto the disk with no reboot, and fs-verity is enabled on the
  objects afterwards (live boot with `composefs.digest=?…`, verity
  optional). It proved Secure Boot, measured boot, TPM2 unlock and `bootc
  upgrade` on composefs. Against this track: the live root runs without
  enforced fs-verity (this medium enforces it live), the migrate took 2 min
  51 s for about 12 GB against 49 s for this track's block copy, repart's
  `BlockDeviceReplace=` fails with a seed attached, and `bootc status` fails
  in the live session until the first reboot. Its other findings, checked
  against this track: `flatpak-preinstall.service` filling the live RAM
  applied to Utah and is fixed (skipped under `utah.live`);
  `ublue-booted-image.service` exists in Utah but failed on no live boot;
  Utah has no remove-installer service; and the `EFI/BOOT` fallback boot
  does not apply, because `install.sh` creates a "Utah" firmware entry for
  `EFI/fedora/shimx64.efi`, first in the boot order (run
  [44897756832](https://github.com/projectbluefin/utah/actions/runs/44897756832)).
  Converging both spikes on one medium and two install modes (copy,
  migrate) is a decision for a later ADR.

## Comparison criteria and how the track measures them

`image-installer.yml` writes the same metrics for both paths, for the **same
`:testing` digest**, to the job summary and a `comparison.json` artifact. The
fisherman side is read from the latest Post-Testing E2E `luks` job whose name
carries that digest (job conclusion, phase markers in its log, the `ISO
ready` line, the `production-iso-<image>` artifact size, step timings).

| Criterion | Image-based track | ISO/fisherman (`luks` job) |
|---|---|---|
| Medium size | raw apparent, allocated, zstd -3 bytes | ISO size from the build log; production ISO artifact bytes |
| Build time | image layer + `bootc install to-filesystem` | "Compose disposable debug ISO" step |
| Install time | `install.sh` total (and GUI Confirm-to-done) | phase 3 to phase 4 markers |
| Per-stage pass/fail | live boot, installer, unlock, initial setup, desktop, switch, rollback | phases 1-7 |
| SELinux mode | live and every installed boot (`getenforce`) | live: permissive by construction (#22); installed: not recorded |
| Failed units | `systemctl --failed` on every boot | not recorded (contract services checked instead) |
| Reliability | pass rate of nightly runs (#1403) | pass rate of Post-Testing E2E `luks` |

## Exit criteria

**Switch** (a new ADR supersedes this one, then a separate PR moves the
promotion gate):

- the track passes on 14 consecutive nightly runs for the main flavor and on
  a dispatch over every flavor in `config/flavors.json`;
- the installed disk passes the checks the `luks` job applies today (offline
  payload, default Flatpaks offline, contract services, desktop login), ported
  to or run against the image path;
- #1404 (hybrid medium) is done with Secure Boot through the upstream chain,
  and #1405 is decided;
- install time and medium size are no worse than fisherman's for the same
  digest;
- the owner accepts it.

**Abandon** (mark this ADR Rejected and delete the track):

- bootc can no longer upgrade or roll back a copied disk and upstream will not
  support it;
- a bootable hybrid or USB medium needs a Utah-owned Secure Boot key;
- the track fails intrinsically (not infrastructure) on more than half of its
  nightly runs over 30 days.
