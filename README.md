# Utahraptor
†Utahraptor ostrommaysi

Bluefin built on Fedora Hummingbird. The more ... civilized murder machine. 

> Your day keeps getting worse.





![alt](https://github.com/user-attachments/assets/962af585-6e2a-4038-ac14-8e54a3189420)


![alt](https://github.com/user-attachments/assets/56428338-54a0-4376-a53b-5f02f8b101a1)

**Experimental pre-alpha** — this does not have users yet, it has a build. No
image has been published, there is no installer and no ISO. Nothing here is
ready to run on a machine you care about. [Filing
issues](https://github.com/projectbluefin/utah/issues) is the whole point.

## What it is

[Bluefin](https://projectbluefin.io) built on [Fedora
Hummingbird](https://packages.redhat.com), which supplies a hardened, fast-moving
bootable base and no desktop at all. Utah adds the desktop: Bluefin's package
contract on top, and the GNOME 51 stack built from source because neither
Hummingbird nor Fedora 44 ships it.

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

## Package parity with Bluefin

`packages/bluefin.toml` is a byte-for-byte copy of Bluefin's `base.toml`, and CI
diffs it against upstream on every run, so drift fails the build rather than
being noticed later.

| | count |
| --- | --- |
| Bluefin contract installed | **64** |
| Utah additions (GNOME 51, desktop services) | 12 |
| Genuinely unavailable | **1** |

The install writes its resolved list to `/usr/share/utah/contract.txt` and the
verify step asserts *that file*, so the two cannot disagree.

## Known gaps

This is the honest list, and it is why the label above says pre-alpha.

- **Nothing is published.** No image, no ISO, no installer. An ISO is planned as
  a fork of [dakota-iso](https://github.com/projectbluefin/dakota-iso).
- **The NVIDIA and gaming flavors are unproven.** The OGC kernel compiles with
  `sched_ext` and `binderfs` genuinely enabled, and the NVIDIA open module
  compiles for the base kernel. The module against the OGC kernel, the driver
  installer flags, and the flavored builds pulling the kernel cache image have
  not yet all passed in one run.
- **`pipewire-libs-extra` is missing.** It was never a Fedora package; it exists
  only in negativo17's `fedora-multimedia`, which Bluefin enables for its whole
  install.
- **Codec support differs.** Twelve `[multimedia_overrides]` names are packages
  Fedora already ships and Bluefin *replaces* with negativo17 builds. Utah
  installs Fedora's. Nothing is absent from the image; hardware-accelerated
  codecs are what differ. `utah-packages` already builds several of them, so
  this closes when Utah consumes that overlay.
- **The image is still pre-alpha.** The digest-pinned `utah-packages` OCI
  repository is consumed and the local QEMU image reaches GDM and GNOME Shell.
  Initial single-architecture live ISO bring-up is now scaffolded; installer
  payload/offline installation integration is still pending.
- **CUDA is deliberately excluded** — 7.68 GB installed. Use the NVIDIA
  container toolkit, which is included, and run CUDA in a container.

See the [open issues](https://github.com/projectbluefin/utah/issues) for where
things stand.

## Contributing or building from source

See [docs/building.md](docs/building.md) for what the image is made of, how to
build it locally, and how the flavor set and kernel cache work.
