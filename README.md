# Utahraptor

<!-- BEGIN E2E VERIFICATION -->
[![Verified install desktop](https://github.com/projectbluefin/utah/raw/4293e1fa7dde4965ae3122bdc179cffe2c31ba34/screenshots/14-first-boot-desktop.png)](https://github.com/projectbluefin/utah/blob/4293e1fa7dde4965ae3122bdc179cffe2c31ba34/README.md)

*gnome-setup install test passed for commit `baa9948dc72e`. [CI run](https://github.com/projectbluefin/utah/actions/runs/45957124460) also runs independent desktop tests that do not gate this record; every flavor installed, booted, switched and rolled back; [screenshots and provenance](https://github.com/projectbluefin/utah/tree/4293e1fa7dde4965ae3122bdc179cffe2c31ba34).*
<!-- END E2E VERIFICATION -->

†Utahraptor ostrommaysi

Bluefin built on Fedora Hummingbird. The more ... civilized murder machine.

> Your day keeps getting worse. Why are there more.

**Experimental pre-alpha** — the image builds, boots, and installs end to
end in QEMU validation: the live medium boots, GNOME STF's gnome-setup
installs it by block copy into a LUKS2 disk, and every flavor's installed
system boots to gnome-initial-setup and a GNOME session, then switches and
rolls back with bootc (verified record: [verification](https://github.com/projectbluefin/utah/tree/verification)).
Testing container images and install media are available for hardware testing,
but nothing here is ready to run on a machine you care about yet. [Filing
issues](https://github.com/projectbluefin/utah/issues) is the whole point.

## What it is

[Bluefin](https://projectbluefin.io) built on
[Fedora Hummingbird](https://fedoramagazine.org/fedora-hummingbird-linux-taking-the-hummingbird-model-to-the-full-os/) —
a minimal, hardened bootable container base that ships without a desktop. Utah
adds the Bluefin experience and a GNOME 51 desktop compiled from upstream
source.

![Utahraptor size comparison: three specimen silhouettes compared with a human](https://github.com/user-attachments/assets/56428338-54a0-4376-a53b-5f02f8b101a1)

Utah is built across two repositories:

| Repository | What it does |
| --- | --- |
| [`projectbluefin/utah`](https://github.com/projectbluefin/utah) | Assembles the bootable OS image and live ISO. |
| [`projectbluefin/utah-packages`](https://github.com/projectbluefin/utah-packages) | Compiles GNOME 51 and desktop packages from source into an OCI repository. |

## Download and image streams

- **Install medium (gnome-setup, Secure Boot off):** [`ghcr.io/projectbluefin/utah-installer`](https://github.com/projectbluefin/utah/pkgs/container/utah-installer) (`testing` tag), published weekly and on demand from `:testing` — a raw disk image that boots live and installs with GNOME STF's gnome-setup ([docs/adr/0001](https://github.com/projectbluefin/utah/blob/main/docs/adr/0001-image-based-installer.md)), baked Flatpaks included. A projectbluefin.dev download from `:stable` follows when [projectbluefin/dakota-iso#414](https://github.com/projectbluefin/dakota-iso/pull/414) publishes it.

  ```sh
  # Fetch the OCI artifact, verify its checksum, decompress, then flash it.
  oras pull ghcr.io/projectbluefin/utah-installer:testing
  sha256sum -c medium.zst.sha256
  zstd -d --long=31 medium.zst -o utah-installer.raw
  sudo dd if=utah-installer.raw of=/dev/sdX bs=4M status=progress conv=fsync
  ```

  `--long=31` is required to decompress, because the medium was compressed with it.
- **Older live ISO (fisherman installer, retiring):** [`utah-live-latest.iso`](https://projectbluefin.dev/utah-live-latest.iso) ([SHA-256 checksum](https://projectbluefin.dev/utah-live-latest.iso-CHECKSUM)), last built from `:stable` by projectbluefin/dakota-iso; Utah no longer tests this installer (#1569)
- **Live versions & downloads:** [docs.projectbluefin.io/utah](https://docs.projectbluefin.io/utah)

Downloads on projectbluefin.dev are built by
[`projectbluefin/dakota-iso`](https://github.com/projectbluefin/dakota-iso), the
repository that builds the install media for Utah and Dakota. Verify every
download against its checksum before use.

Like Bluefin, Utah builds four hardware flavors (`utah`, `utah-nvidia`,
`utah-gaming`, `utah-nvidia-gaming`) from `config/flavors.json`:

| Tag | Stream | Status |
| ---: | ---: | :--- |
| `:testing` | Testing | Published at `ghcr.io/projectbluefin/utah:testing` after every flavor passes the gnome-setup install test and the testsuite. |
| `:stable` | Stable | Rolling tag, moved from `:testing` on each promotion to `main` after the release gate passes on the exact candidate digests, and by a daily check once `:stable` is over a week old and `:testing` has moved on. Pre-alpha like everything here. |

## Parity with Dakota

Utah's parity target is [Dakota](https://github.com/projectbluefin/dakota), the
Bluefin built on GNOME OS (`ghcr.io/projectbluefin/dakota:testing`).
[`baselines/GAP.md`](baselines/GAP.md) lists every user-visible file Dakota
ships that Utah lacks, and each one is triaged in `baselines/triage.toml`;
structural differences (bootloader, security framework, swap, shells, service
policy) are tracked under [#1236](https://github.com/projectbluefin/utah/issues/1236).

`packages/base.toml` is Utah's own base manifest. It started as a copy of
Bluefin's `base.toml` and is now edited by hand as Dakota parity work lands.

| | count |
| --- | --- |
| Base manifest installed | **57** |
| Utah additions (GNOME 51, base-image parity, device firmware, desktop services) | 375 |
| Genuinely unavailable | **5** |

## Known gaps

Why this is still pre-alpha:

- **Disable Secure Boot.** No flavor boots with Secure Boot on yet. Every flavor runs Fedora's kernel rebuilt with a btrfs fs-verity fix, and that rebuild is not Fedora-signed. The gaming flavors' OGC kernel, the NVIDIA module, `v4l2loopback` and the live ISO's `systemd-boot-unsigned` are unsigned too. Utah will not sign them with its own key or ask you to enroll a MOK. Signing comes from upstream instead: the kernel fix going into Fedora's signed kernel, and signed systemd-boot and kernel modules through Hummingbird (#1225, #1379, #1380, #1381). Boot via UEFI with Secure Boot off, and flash the ISO directly with Fedora Media Writer or `dd` (Ventoy and legacy BIOS are unsupported). The live session runs SELinux permissive; installed systems run Enforcing.
- **Switching from another `bootc` image.** Ostree's 3-way `/etc` merge can carry over `bootc-fetch-apply-updates.timer` from another image. Utah masks `bootc-fetch-apply-updates` so background updates go through `uupd` instead of silently undoing a `bootc rollback`. After switching, check `systemctl is-enabled bootc-fetch-apply-updates.timer`.
- **Bootloader updates.** Systems on `systemd-boot` update their EFI partition via `bootctl` (`systemd-boot-update.service`), so `bootupctl status` intentionally shows no components. Check `bootctl status` instead.
- **Hardware, codecs, and drivers need real-world testing.** Intel/Linux Wi-Fi firmware and Intel media drivers are included, and the OGC kernel (`sched_ext`, `binderfs`) and open NVIDIA module compile, but hardware video decoding, Wi-Fi, and gaming flavors still need testing on physical machines (#383).
- **The Wi-Fi password prompt can freeze the desktop on some laptops.** On at least one Intel AX201 machine, typing into the desktop's Wi-Fi password dialog stops GNOME Shell responding; the kernel and Wi-Fi stack keep running. Connect from a terminal instead: `sudo nmcli --ask device wifi connect <SSID>` (#354).
- **CUDA is excluded** to save 7.68 GB in the base image. Use the included NVIDIA Container Toolkit to run CUDA inside containers.
- **Dakota parity is incomplete.** Dakota ships kanata, systemd-boot with a signed chain, AppArmor, oo7, sudo-rs, libvirt/QEMU and more that Utah does not yet; see [`baselines/GAP.md`](baselines/GAP.md) and #1236.

See the [open issues](https://github.com/projectbluefin/utah/issues) to track progress or report bugs.

## Contributing

See [docs/building.md](docs/building.md) to build locally, [docs/skills/](docs/skills/) for architecture docs, and [AGENTS.md](AGENTS.md) for agent rules.
