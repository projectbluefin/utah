# Secure Boot: what is unsigned and where it gets signed

Owner decision (2026-11-20, #1225): Utah does **not** sign anything with a key
of its own, and users never enroll a Utah MOK, unlike Dakota. Everything on the
Secure Boot path must be signed by the chain the base image already trusts,
which means getting each piece upstream.

That chain is **Fedora's**, not Hummingbird's. `os-minimal` takes `shim-x64`,
`grub2-efi-x64` and `kernel` from `fedora-44-updates`. Its README says
"Fedora complements the kernel and lower-level stack", and Hummingbird's rpms
repository has no kernel, shim or GRUB package and signs no EFI binary.
Hummingbird builds `systemd` (including `systemd-boot-unsigned`). Its kernel
import is a draft (redhat/hummingbird/rpms!4636).

## Inventory (ghcr.io/projectbluefin/utah:testing, sha256:0578bcb6, 2026-11-19)

Collected with `sbverify --list` on the EFI binaries and `modinfo` plus the
module signature trailer on `.ko` files. Run the same against a new image to
re-check.

| Component | Flavors | Signed by | Shim / lockdown verdict |
| --- | --- | --- | --- |
| `shimx64.efi` (`shim-x64` 16.1-7, Fedora) | all, installed | Microsoft UEFI CA 2011 | OK |
| `grubx64.efi` (`grub2-efi-x64` 2.12-66.fc44, Fedora) | all, installed | Fedora Secure Boot CA 20250530 | OK |
| Kernel `7.3.3-200.1.utah1.fc44` (Fedora SRPM rebuilt with the btrfs fix, `scripts/install-fedora-kernel.sh`) | all | pesign's **Red Hat Test Certificate** | **refused by shim** |
| In-tree modules of that kernel | all | the rebuild's own per-build key | follow the kernel |
| OGC kernel `7.1.8-ogc1` (`scripts/install-ogc-kernel.sh`) | gaming, nvidia-gaming | none; `CONFIG_MODULE_SIG` unset | **refused by shim** |
| NVIDIA open `nvidia*.ko` 595.99.02 (`scripts/install-nvidia.sh`) | nvidia, nvidia-gaming | none | **refused by lockdown** |
| `v4l2loopback.ko` 0.15.4 (`scripts/install-v4l2loopback.sh`) | all (loaded on demand) | none | **refused by lockdown** (OBS virtual camera only; boot unaffected) |
| `systemd-bootx64.efi` (`systemd-boot-unsigned` 261.2-1.1.hum1, Hummingbird) | all (bootctl updates); live ISO `EFI/BOOT/BOOTX64.EFI` | none | **refused by firmware** |
| UKIs | none shipped | n/a | n/a |

The installed chain is shim → GRUB → kernel, as `bootc-installer` writes it
(`"bootloader": "grub2"`). It breaks at the kernel. The live ISO (`just iso`,
and dakota-iso's `utah-live.iso` built from `:stable`) breaks at its first
binary.

## Route per piece

| Piece | Route | Submission | Tracking |
| --- | --- | --- | --- |
| btrfs fs-verity fix (drop Utah's kernel rebuild) | Into the **Fedora** kernel that `os-minimal` ships: kernel-ark `fedora-7.3` backport and a stable request. The same fix is offered as a patch on Hummingbird's draft kernel import | [hanthor/hummingbird-rpms#2](https://github.com/hanthor/hummingbird-rpms/pull/2); kernel-ark and stable texts prepared | #1379 |
| Signed systemd-boot | Hummingbird imports Fedora's `systemd-boot` signing wrapper and signs it with a CA its shim trusts. As filed, the build signs with pesign's Red Hat Test Certificate and puts the shim hardlink in `EFI/hummingbird/` while the shim is in `EFI/fedora/`, so it needs a real Hummingbird signing step (ADR-0005). Open question to Hummingbird: is a signed systemd-boot in scope, and with which CA, or do derived images wait for Fedora 45's `systemd-boot-x64`? Fallback: Fedora's signed `systemd-boot-x64` once the base is Fedora 45, or an ISO ESP of Fedora shim + GRUB | [redhat/hummingbird/rpms!5683](https://gitlab.com/redhat/hummingbird/rpms/-/merge_requests/5683) (open; staged as the closed mirror PR [hanthor/hummingbird-rpms#3](https://github.com/hanthor/hummingbird-rpms/pull/3)) | #1380 |
| NVIDIA open and v4l2loopback modules; signing a Hummingbird kernel | Hummingbird decides whether it signs kernels and kmods (ADR-0005). Kmod packages need a Hummingbird kernel with a persistent module key first | [hanthor/hummingbird-documentation#2](https://github.com/hanthor/hummingbird-documentation/pull/2) | #1381 |
| OGC kernel | Not acceptable upstream: a third-party fork that neither Fedora nor Hummingbird signs. Stays unsigned and Secure-Boot-off. The open alternative is to boot Fedora's signed kernel, which has sched_ext, ntsync and Rust binder | none | #1225 |

The GitHub repositories are hand-refreshed mirrors of the GitLab ones, used to
stage the proposals. The GitLab MRs follow once Hummingbird picks a route.

## When a piece lands

- **Fedora kernel with `254e15591ee4`** (a 7.3.y stable with the backport, a
  kernel-ark `fedora-7.3` build, or the 7.4 rebase) reaches `BASE_IMAGE`:
  remove the rebuild as [kernel-cache.md](../../kernel-cache.md#the-patched-fedora-kernel)
  describes. `main` and `nvidia` then boot Fedora's signed kernel through
  Fedora's shim and get sched_ext back.
- **Signed systemd-boot** is available: replace `systemd-boot-unsigned` in
  `packages/utah.toml`, drop `ConditionSecurity=!uefi-secureboot` from
  `systemd-boot-update.service.d/10-only-on-systemd-boot.conf`, and give the
  live ISO a shim-first ESP.
- **Signed kmods** exist: replace `install-nvidia.sh`'s module build and
  `install-v4l2loopback.sh` with the packages.
- Update the README's "Disable Secure Boot" bullet and this table each time.
