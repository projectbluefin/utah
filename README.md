# Utahraptor

<!-- BEGIN E2E VERIFICATION -->
[![Verified end to end](docs/verification/screenshots/installed-fastfetch.png)](docs/verification/README.md)

*Verified end to end on 2026-09-06T18:12:30Z: installed to a LUKS2-encrypted disk, unlocked at the Plymouth prompt, and logged in to a GNOME session — the shot above is fastfetch inside that booted install. Full record and more screenshots in [docs/verification](docs/verification/README.md), refreshed by `just luks-test`.*
<!-- END E2E VERIFICATION -->

†Utahraptor ostrommaysi

Bluefin built on Fedora Hummingbird. The more ... civilized murder machine.

> Your day keeps getting worse. Why are there more.

![alt](https://github.com/user-attachments/assets/56428338-54a0-4376-a53b-5f02f8b101a1)

**Experimental pre-alpha** — the image builds, boots, and reaches GDM in local
QEMU validation, and a single-architecture UEFI live ISO with an embedded
offline installer payload builds and validates locally (`just iso`, `just boot-iso`,
`just luks-test`). However, no images have been published to a public registry,
and no official ISO release artifacts have been published. Nothing here is ready
to run on a machine you care about. [Filing
issues](https://github.com/projectbluefin/utah/issues) is the whole point.

## What it is

[Bluefin](https://projectbluefin.io) built on [Fedora
Hummingbird](https://packages.redhat.com), which supplies a hardened, fast-moving
bootable base and no desktop at all. Utah adds the desktop: Bluefin's package
contract on top, and the GNOME 51 stack built from source because neither
Hummingbird nor a Fedora release ships it.

Utah installs runtime packages strictly from its Hummingbird base and overlay
plus the pinned `utah-packages` factory repository. Fedora and Rawhide
repositories are never enabled at runtime (Fedora 44 is used strictly as a
builder-stage toolchain for kernel cache builds).

<img src="https://github.com/user-attachments/assets/962af585-6e2a-4038-ac14-8e54a3189420" alt="alt" width="40%">

Two repositories, the way `common` and `brew` already work:

| Repository | What it does |
|---|---|
| [`projectbluefin/utah`](https://github.com/projectbluefin/utah) | This one. Composes the image. |
| [`projectbluefin/utah-packages`](https://github.com/projectbluefin/utah-packages) | Builds GNOME 51 and the rest of the desktop stack from verified upstream sources, and publishes them as an OCI image. |

## Image streams

| Tag | Stream | What it is |
| ---: | ---: | ---: |
| `:testing` | Dev | Built from `testing`, advanced only after end-to-end validation. |
| `:stable` | Stable | Promoted from `:testing`. |

Four flavors per stream — `utah`, `utah-nvidia`, `utah-gaming`,
`utah-nvidia-gaming` — matching Bluefin's. `config/flavors.json` is the single
source for that set, for the promote and release matrices, and for whether the
kernel cache image gets built at all.

**None of these are published yet.** The tags above describe what the pipeline
is built to produce, not something you can pull today.

## Status: local implementation vs public availability

| Area | Local implementation status | Public distribution status |
|---|---|---|
| **OCI image composition** | Implemented (`just build-ghcr utah testing main` or `just build-local`) | Not yet published to GHCR |
| **Installed VM execution** | Implemented (`just generate-bootable-image testing && just boot-vm`); reaches GDM and GNOME Shell | No pre-installed disk images distributed |
| **Live ISO & installer** | Implemented (`just iso testing && just boot-iso`, `just luks-test`); UEFI ISO with systemd-boot, embedded `bootc-installer` Flatpak, and offline OCI overlay payload | No official ISO release artifacts published |

## Package parity with Bluefin

`packages/bluefin.toml` is a byte-for-byte copy of Bluefin's `base.toml`, and CI
diffs it against upstream on every run, so drift fails the build rather than
being noticed later.

| | count |
| --- | --- |
| Bluefin base contract installed | **59** |
| Utah additions (11 GNOME 51, 28 parity, 5 desktop services) | **44** |
| Total verified contract packages | **103** |
| Genuinely unavailable (deferred parity debt) | **9** |

*Note: Total verified contract packages count is 103 on standard flavors (including the Fedora 44 release-specific package `gnupg2-scdaemon`), with +1 NVIDIA package (`nvidia-container-toolkit`) on NVIDIA flavors for 104.*

The install writes its resolved list to `/usr/share/utah/contract.txt` and the
verify step asserts *that file*, so the two cannot disagree.

### Documented unavailable packages (parity debt)

Every package Bluefin ships that is absent from Utah's enabled runtime
repositories is tracked under `[unavailable]` in `packages/utah.toml`:

- **`anaconda-live`** (#83) — Utah's ISO uses `bootc-installer`. `anaconda-live`
  requires `anaconda-webui`, which is excluded because Cockpit authentication
  requires a working GHmac backend in Hummingbird's GLib.
- **`evolution-ews-core`** (#10) — Exchange Web Services plugin for Evolution.
  Requires the entire `evolution-data-server` dependency closure (camel,
  evolution-mail, evolution-shell, libebook, libecal), which the factory does
  not yet build.
- **`firefox`** (#35, utah-packages#112) — Bluefin's desktop contract names the
  Firefox RPM, but the factory only carries `mozjs140`. Utah ships Firefox via
  Flatpak in desktop and live installer configurations instead.
- **`fish`** (utah-packages#19) — Upstream spec uses `%cargo_generate_buildrequires`,
  expecting Rust crates as individual system RPMs that do not exist in the
  buildroot. Needs a vendored/cargo-fetch rebuild.
- **`libgda`** (#106, projectbluefin/bluefin#1210) — Runtime dependency for the
  maintained Copyous extension. Neither in the factory image nor in Hummingbird
  RPMs.
- **`libgda-sqlite`** (#106, projectbluefin/bluefin#1210) — SQLite provider
  subpackage of `libgda` for Copyous.
- **`ppp`** (#107) — In neither repository; required by Hummingbird's
  `NetworkManager-ppp` plugin.
- **`slitherer`** (#83) — Alternative installer that requires `anaconda-webui`
  and lacks its Qt WebEngine dependency closure.
- **`zsh`** (utah-packages#21) — HTML docs build pulls texi2html, causing a file
  conflict in the buildroot between Hummingbird's ruby4.0 and Fedora's ruby.

## CI/CD pipeline and Dakota-style quality gates

Utah adopts Dakota-style quality gates to validate every change before
promotion or publication:

1. **Preflight contract gate** — `just check` validates manifests, workflow
   mappings, download integrity checksums, desktop contract rules, and extension
   metadata. `just check-parity` verifies `packages/bluefin.toml` against
   upstream Bluefin, and `just check-repos` confirms package availability in
   runtime repositories before any container build runs.
2. **Build gate** — `build.yml` splits the build matrix into `build_main`
   (pristine Hummingbird base) and `build_kernel` (built on the content-hashed
   kernel cache image). Builds run with read-only layer caching on PRs.
3. **Artifact verification gate** — Images are validated with in-image verifiers:
   `verify-rpm-contract.py` checks that every package resolved in
   `/usr/share/utah/contract.txt` is installed and validates package-origin
   attestations (`.bfin` identity); `verify-desktop-contract.py` verifies Utah
   branding, os-release identity (`NAME=Utah`, `ID=utah`, `VERSION_CODENAME=Utahraptor`),
   and service presets; `verify-gnome-extensions.py` verifies GNOME 51
   compatibility for all bundled extensions. Signed artifacts carry cosign
   keyless signatures, SBOMs, and SLSA provenance.
4. **End-to-end VM & installer testing gate** — `just generate-bootable-image`
   and `just boot-vm` exercise full `bootc install to-disk` and QEMU boot to GDM.
   `just iso` and `just boot-iso` validate live boot and offline installation,
   while `just luks-test` verifies end-to-end encrypted installation without
   network. CI runs `.github/workflows/post-testing-e2e.yml` on testing builds
   before advancing `:testing`.
5. **Promotion and rollback lifecycle** — The promotion workflow (`.github/workflows/promote-testing-to-main.yml`)
   is triggered by a push to `testing` (or a nightly schedule) and squash-promotes
   `testing` to `main`. The `:stable` image tag is cut separately by
   `.github/workflows/execute-release.yml`. Pushes to `main` trigger
   `.github/workflows/sync-main-to-testing.yml` to keep the integration branch
   in sync. Deployed systems can roll back at any time with standard `bootc rollback`.

## Known gaps

This is the honest list, and it is why the label above says pre-alpha.

- **Public distribution is pending.** No OCI image or ISO artifact has been
  published to a public registry or release download. The local build, VM boot,
  and live ISO with embedded offline installer payload (`org.bootcinstaller.Installer`
  Flatpak and target OCI image in an overlay `containers-storage` graphroot) are fully
  implemented and validated end-to-end.
- **Cross-vendor switch and update timers (`bootc-fetch-apply-updates`).**
  Switching to Utah from Bluefin or other bootc images carries Bluefin's
  `/etc/systemd/system/timers.target.wants/bootc-fetch-apply-updates.timer`
  symlink across ostree's 3-way `/etc` merge. Utah masks
  `bootc-fetch-apply-updates.timer` and `bootc-fetch-apply-updates.service` in
  both `/etc` and `/usr/lib/systemd/system/` (and presets them to disabled) so
  background auto-updates do not bypass `uupd` policy or silently undo a
  rollback (`bootc rollback`). Switchers should verify with
  `systemctl is-enabled bootc-fetch-apply-updates.timer` and can re-assert the
  mask (`systemctl mask --now bootc-fetch-apply-updates.timer bootc-fetch-apply-updates.service`)
  if a merged `/etc` wants symlink remains on disk (links #17, #101).
- **The NVIDIA and gaming flavors are unproven.** The OGC kernel compiles with
  `sched_ext` and `binderfs` genuinely enabled, and the NVIDIA open module
  compiles for the base kernel. The module against the OGC kernel, the driver
  installer flags, and the flavored builds pulling the kernel cache image have
  not yet all passed in one run.
- **Hardware-accelerated codec support differs.** Twelve `[multimedia_overrides]`
  names are packages Hummingbird already ships and Bluefin *replaces* with
  negativo17 builds. Utah installs Hummingbird's builds of those packages;
  Fedora and negativo17 repositories are never enabled at runtime. Hardware-accelerated
  codecs differ until Utah consumes the `utah-packages` overlay for them.
- **The image is still pre-alpha.** The digest-pinned `utah-packages` OCI
  repository is consumed and the local QEMU image reaches GDM and GNOME Shell.
- **CUDA is deliberately excluded** — 7.68 GB installed. Use the NVIDIA
  container toolkit, which is included, and run CUDA in a container.

See the [open issues](https://github.com/projectbluefin/utah/issues) for where
things stand.

## Contributing or building from source

See [docs/building.md](docs/building.md) for how to build the image locally,
and [docs/skills/](docs/skills/) for the deep documentation. Agents start at
[AGENTS.md](AGENTS.md).
