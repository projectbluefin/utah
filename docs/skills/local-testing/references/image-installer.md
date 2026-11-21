# Image-based installer track (experimental)

The GNOME OS-style install path of
[ADR-0001](../../../adr/0001-image-based-installer.md), run in parallel with
the live ISO + bootc-installer/fisherman path. Nothing gates on it: it is not
a required check, not in `promote-to-testing`'s needs, and it publishes
nothing. Tracking issue: #1403. History: spike #1399.

## What it builds

| Piece | File |
|---|---|
| Live-capable layer on a published digest: OS-Installer from a pinned `hanthor/os-installer` commit (`OSI_REF`, fetched by commit and compared after checkout), `utah.live` units, initramfs with `systemd-volatile-root` | `iso/raw/Containerfile`, `iso/raw/src/` |
| Raw medium: GPT BIOS-BOOT, 512 MiB ESP, 1 GiB ext4 `/boot`, btrfs root; one `bootc install to-filesystem --generic-image --karg rhgb --karg quiet`; "Utah Live" BLS entry as GRUB default; `EFI/BOOT` fallback | `iso/raw/build-raw-image.sh`, `iso/raw/medium.py` |
| Offline Flatpaks in the medium's stateful `/var` (stateroot `var/lib/flatpak`), never in an image: `flatpak preinstall` of the image's own `preinstall.d` set, run in the image with that directory bind-mounted, then `setfiles` with the image's contexts | `iso/raw/bake-flatpaks.sh` |
| Shrink to content: fstrim, btrfs balance + resize to minimum (+256 MiB), cut the root partition and the file, rewrite the backup GPT; `size-breakdown.txt` shows what takes the space | `iso/raw/shrink-raw-image.sh`, `medium.py shrink-plan` |
| End to end with screenshots and `evidence/results.json` | `iso/raw/raw_e2e.py`, `atspi_drive.py`, `vte_term.py` |
| Size per step (raw, allocated, zstd -3, zstd -19 --long, xz -9, pack/unpack time) | `iso/raw/measure_medium.py` |
| Comparison with the Post-Testing E2E `luks` job for the same digest | `iso/raw/compare_paths.py` |

The installer's own logic (`utah_config/scripts/install.sh`: mirror the
medium's GPT with `CopyBlocks=`, LUKS2 via `Encrypt=key-file`, new filesystem
UUIDs and their references, drop the live entry, `rd.luks.uuid` + crypttab,
grow root) lives in the os-installer fork. Bump it by moving `OSI_REF` in
`iso/raw/Containerfile` to a full commit of the fork's `utah` branch.

### Offline Flatpaks

The published image and the live layer stay Flatpak-free; the workflow
fails if either has content in `/var/lib/flatpak`. bootc copies an image's
`/var` only at the first deployment and never touches it again, so Flatpaks
in the medium's stateroot `/var` reach the installed disk through the block
copy and can never be re-added or overwritten by an update. `flatpak
preinstall` records them as preinstalled, the state flatpak-preinstall.service
leaves on a fisherman install, so a removed app stays removed. The E2E proves
both: every installed boot checks the baked list (`flatpak-apps.txt`), and an
app removed after first boot (`--remove-flatpak`, default
`org.gnome.Calculator`) must still be absent after switch and rollback, once
flatpak-preinstall.service has run with the network up.

Each run also measures the same medium without Flatpaks (`size-2-no-flatpaks.json`)
so the comparison shows what the offline set costs in download size.

## Running it

`.github/workflows/image-installer.yml` runs nightly (07:41 UTC, after the
testing build and Post-Testing E2E), on `workflow_dispatch`, and on pull
requests that touch `iso/raw/**`, the workflow, or
`tests/test_image_installer.py`. Dispatch inputs: `image_ref` (exact
`ghcr.io/projectbluefin/<image>@sha256:...`), `all_flavors`, `install`
(`gui` drives OS-Installer's window through a QEMU USB tablet at AT-SPI
extents; `script` runs the same `install.sh` over SSH), `btrfs_compress`
(build the root with `compress-force=<value>`), `formats`.

The job summary holds the comparison table; the
`image-installer-<image>` artifact holds screenshots, `contact-sheet.png`,
evidence, raw-evidence and `comparison.json`. The medium and the disks are
never uploaded: CI media carry a root SSH key, `utah.live.debug` (live user
password `live`, sshd) and the test passphrase.

Locally, with a published digest and root (KVM, podman, qemu, OVMF, zstd, xz):

```bash
sudo podman build -t localhost/utah-raw:ci --build-arg SOURCE_IMAGE=ghcr.io/projectbluefin/utah@sha256:... -f iso/raw/Containerfile iso/raw
sudo bash iso/raw/build-raw-image.sh localhost/utah-raw:ci output/medium.raw ghcr.io/projectbluefin/utah:testing
sudo bash iso/raw/bake-flatpaks.sh localhost/utah-raw:ci output/medium.raw   # needs Flathub
sudo bash iso/raw/shrink-raw-image.sh localhost/utah-raw:ci output/medium.raw
```

`dd` the shrunk `medium.raw` to a USB stick (UEFI only; no El Torito yet,
#1404). Without `UTAH_RAW_LIVE_KARGS=utah.live.debug` the live user has no
password and there is no sshd.

## What the comparison measures

| Metric | Image-based | ISO/fisherman (`luks` job, same digest) |
|---|---|---|
| Medium | raw and allocated bytes after bootc install and after shrinking; compressed bytes and pack/unpack time per format | `ISO ready` line; `production-iso-<image>` artifact bytes |
| Build time | image layer + `bootc install` | "Compose disposable debug ISO" step |
| Install time | `install.sh` total and block copy (`utah-install.log`), GUI confirm-to-done | `=== Phase 3/7` to `=== Phase 4/7` |
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
