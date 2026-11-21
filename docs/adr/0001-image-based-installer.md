# ADR-0001: Image-based installer (GNOME OS-style), in parallel with fisherman

- **Status:** Proposed
- **Date:** 2026-11-21
- **Tracking:** #1403 (track and comparison runs), epic #1236 (Dakota parity)
- **Follow-ups:** #1404 (hybrid ISO, systemd-boot/UKIs), #1405 (TPM2, recovery
  key), #1406 (Dakota branches)
- **History:** feasibility spike #1399 (not merged; superseded by the track)

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
   and a btrfs root (`iso/raw/build-raw-image.sh`).
2. **It boots live with one karg, `utah.live`.** A "Utah Live" BLS entry is
   the medium's default. In the initrd, `utah-live-root.service` runs
   `systemd-volatile-root overlay /sysroot` before `ostree-prepare-root`, so
   the live session writes to RAM and the medium stays byte-for-byte what is
   installed. `!utah.live` drop-ins keep updaters and `/boot` writers off.
3. **Install is a block copy into LUKS2**, done today by
   [OS-Installer](https://gitlab.gnome.org/p3732/os-installer) with Utah's
   config and `install.sh` from
   [hanthor/os-installer](https://github.com/hanthor/os-installer)
   (`utah_config/`, pinned by commit): mirror the medium's GPT into
   `repart.d` with `CopyBlocks=`, `Encrypt=key-file` on root, regenerate every
   filesystem UUID and rewrite its references, drop the live entry, add
   `rd.luks.uuid` and crypttab. gnome-initial-setup creates the user on first
   boot.
4. **Port later to a proto-installer-style daemon** (polkit-guarded D-Bus
   service, repart-native TPM2, recovery key) shared by Utah and Dakota, once
   the copy/fix-up logic has settled. Do not grow os-installer's shell
   contract beyond what the comparison needs.
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
  not validity). The live overlay puts composefs at overlay depth 2, the
  kernel maximum, so nested overlay users such as podman's overlay driver are
  not expected to work in the live session (untested).
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

## What remains before it can replace fisherman

1. **Medium format** (#1404): an El Torito hybrid (`systemd-repart
   --el-torito`) with the kernel reachable from the ESP, which means
   systemd-boot and UKIs; the live karg then becomes a medium-only UKI addon.
2. **Offline Flatpaks on the medium** (decision item 6): installed into the
   medium's stateful `/var` at build time from the list the ISO bakes, with
   the image itself kept Flatpak-free.
3. **Encryption UX** (#1405): TPM2 enrollment, recovery key, PCR policy.
4. **Medium layout and `rhgb quiet` in the real build**: today they are set by
   `iso/raw/build-raw-image.sh`, held by tests; they move into the published
   build when the path is adopted.
5. **Dakota branches** (#1406): ESP-resident Type #1 entries, `loader.conf`,
   `LABEL=root`, composefs `state/deploy`, live overlay before
   `bootc-root-setup`.
6. **Installer base**: the proto-installer-style daemon (decision item 4).

## Alternatives considered

- **Keep fisherman/bootc-installer as is.** Known and gating today. Keeps
  every pain point above: install-time `bootc install` (minutes, failure
  surface outside Utah), permissive live session, payload twice on the
  medium. Remains the shipped path until the exit criteria below are met.
- **GNOME OS's proto-installer as is.** Its install logic is GNOME OS
  specific (usr A/B and verity slots, `BlockDeviceReplace=/` of a zram live
  root, `swap-verity`), it has no passphrase, language or keyboard pages, and
  it assumes a TPM. Its skeleton (polkit, D-Bus API, inhibitor, TPM/pcrlock,
  recovery key, OEM mode; MIT) is the long-term base: decision item 4.
- **systemd-sysupdate A/B instead of bootc.** Would replace the updater, the
  `:testing`/`:stable` streams, the promotion and release gates and uupd. The
  spike proved bootc upgrades and rolls back a copied disk, so the updater
  does not need to change for this install path.

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
