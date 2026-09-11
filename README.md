# Utahraptor
†Utahraptor ostrommaysi

Bluefin built on Fedora Hummingbird. The more ... civilized murder machine.

> Your day keeps getting worse. Why are there more.

![alt](https://github.com/user-attachments/assets/56428338-54a0-4376-a53b-5f02f8b101a1)

**Experimental pre-alpha** — the image builds, boots, and reaches GDM in local
QEMU validation, and a single-architecture UEFI live ISO with an embedded
offline installer payload builds locally (`just iso`). However, no images have
been published to a public registry, and no official ISO release artifacts have
been published. Nothing here is ready to run on a machine you care about.
[Filing issues](https://github.com/projectbluefin/utah/issues) is the whole point.

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
| **Live ISO & installer** | Implemented (`just iso testing && just boot-iso`); UEFI ISO with systemd-boot, embedded `bootc-installer` Flatpak, and offline OCI VFS payload | No ISO artifacts released |

## Package parity with Bluefin

`packages/bluefin.toml` is a byte-for-byte copy of Bluefin's `base.toml`, and CI
diffs it against upstream on every run, so drift fails the build rather than
being noticed later.

| | count |
| --- | --- |
| Bluefin base contract installed | **58** |
| Utah additions (10 GNOME 51, 3 desktop services) | **13** |
| Total verified contract packages | **71** |
| Genuinely unavailable (deferred parity debt) | **6** |

The install writes its resolved list to `/usr/share/utah/contract.txt` and the
verify step asserts *that file*, so the two cannot disagree.

### Documented unavailable packages (parity debt)

Every package Bluefin ships that is absent from Utah's enabled runtime
repositories is tracked under `[unavailable]` in `packages/utah.toml`:

- **`evolution-ews-core`** (#10) — Exchange Web Services plugin for Evolution.
  Requires the entire evolution-data-server dependency closure (camel,
  evolution-mail, libebook, libecal), which the factory does not yet build.
- **`firefox`** (#35) — Bluefin's desktop contract names the Firefox RPM, but
  the factory only carries `mozjs140`. Utah ships Firefox via Flatpak in desktop
  and live installer configurations instead.
- **`fish`** (utah-packages#19) — Upstream spec uses `%cargo_generate_buildrequires`,
  expecting Rust crates as individual system RPMs that do not exist in the
  buildroot. Needs a vendored/cargo-fetch rebuild.
- **`grub2-efi-x64-cdboot`** (#35) — Boot-media subpackage absent from enabled
  repositories; Utah uses Hummingbird's bootable base and builds UEFI live media
  with systemd-boot.
- **`pipewire-libs-extra`** (#35) — Absent from Hummingbird and utah-packages;
  Bluefin pulls it from negativo17's `fedora-multimedia`, which Utah does not enable.
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
   `/usr/share/utah/contract.txt` is installed; `verify-desktop-contract.py`
   verifies Utah branding, os-release identity (`NAME=Utah`, `ID=utah`), and
   service presets; `verify-gnome-extensions.py` verifies GNOME 51 compatibility
   for all bundled extensions. Signed artifacts carry cosign keyless signatures,
   SBOMs, and SLSA provenance.
4. **End-to-end VM & installer testing gate** — `just generate-bootable-image`
   and `just boot-vm` exercise full `bootc install to-disk` and QEMU boot to GDM.
   `just iso` and `just boot-iso` validate live boot and offline installation.
   CI runs `.github/workflows/post-testing-e2e.yml` on testing builds before
   advancing `:testing`.
5. **Promotion and rollback lifecycle** — Passing `:testing` images are promoted
   to `:stable` via `.github/workflows/promote-testing-to-main.yml`. Pushes to
   `main` trigger `.github/workflows/sync-main-to-testing.yml` to keep the
   integration branch in sync. Deployed systems can roll back at any time with
   standard `bootc rollback`.

## Known gaps

This is the honest list, and it is why the label above says pre-alpha.

- **Public distribution is pending.** No OCI image or ISO artifact has been
  published to a public registry or release download. The local build, VM boot,
  and live ISO with offline installer payload are fully implemented.
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
