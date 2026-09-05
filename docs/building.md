# Building Utah

Contributor documentation: what the image is made of, how to build it, and how
the flavor set and the kernel cache work. The README is the user-facing page.

## Design

- Four `x86_64` flavors -- `main`, `nvidia`, `gaming`, `nvidia-gaming` --
  matching Bluefin's, on a single `testing` stream. `config/flavors.json` is the
  single source for the build, promote and release matrices; see below.
- Pinned Hummingbird `bootc-os` base, preserving its hardened and fast-moving
  upstream model.
- Pinned `utah-packages` OCI repository, consumed before Fedora packages so
  Hummingbird-targeted rebuilds are actually installed.
- Bluefin's base package manifest is the compatibility contract.
- Bluefin's pinned `projectbluefin/common` and `ublue-os/brew` OCI payloads,
  plus its GNOME Extensions submodules, are retained with their normal build
  step.
- `scripts/configure-services.sh` mirrors bluefin-lts's `40-services.sh`: it
  applies the desktop presets, enables GDM, firmware updates, Tailscale,
  uupd, user setup and resolved, configures authselect, and removes the
  extension build toolchain before cleanup.
- Published images keep SSH disabled. Set `ENABLE_SSHD=1` only for a local
  diagnostic build; this follows TunaOS's debug-image convention and makes
  the disposable root key used by `boot-vm` useful.
- The OGC kernel and NVIDIA's open module are built from source, since neither
  Hummingbird nor UBlue publishes a build for this base. Both are cached in an
  image of their own so the cost is paid once per pin, not once per push; see
  below.
- CI delegates builds, vulnerability reporting, SBOMs, keyless signatures,
  provenance, caching, and rechunking to `projectbluefin/actions@v1`.

## Build locally

```bash
just check
just build-ghcr utah testing main
just generate-bootable-image testing
just boot-vm

# Optional local diagnostics over SSH (never use for a published image):
ENABLE_SSHD=1 just build-ghcr utah testing main
```

The image is tagged `localhost/utah:testing`. `generate-bootable-image` uses
`bootc install to-disk` to create `output/bootable.raw`; `boot-vm` runs that disk
with `ghcr.io/qemus/qemu` and serves the graphical console at the printed URL.
Confirm that GDM starts and the GNOME Shell desktop renders in the web console.
Override `BASE_DIR`, `VM_RAM`, or `VM_CPUS` when needed. `boot-vm` prints the
noVNC and SSH ports; SSH is available only when the image was built with
`ENABLE_SSHD=1`. The generated local disk carries `utah.local`, which skips
unified-storage's registry repull; published images omit that argument and keep
the service enabled.

### Live ISO (initial bring-up)

The first ISO slice reuses Utah's own kernel, dracut-live, and GNOME image:

```bash
just iso testing
just boot-iso             # QEMU/noVNC live-session validation
just debug=1 iso testing   # optional live-session SSH diagnostics
```

The result is `output/utah-live.iso`, assembled with systemd-boot, a
`UTAH_LIVE` dmsquash-live root, and a serial `UTAH_LIVE_READY` marker. It is
intended to prove live desktop boot first; bootc-installer/offline payload
integration is the next ISO milestone.

### Proving an image boots

The Containerfile asserts what an image *contains* -- the package contract, the
desktop contract, the GNOME extensions, `bootc container lint`. None of that can
say whether it starts, and three of Utah's claims live entirely on the other
side of a boot: that a desktop appears at all, that the gaming flavors run the
OGC kernel rather than the stock one, and that the NVIDIA module matches the
kernel that actually booted rather than the one it was compiled against.

`just e2e-boot <image-ref>` answers those. It derives a throwaway layer from the
image (`Containerfile.e2e`), which adds a first-boot unit running
`scripts/verify-boot.sh`, installs that to a disk with `bootc install to-disk`,
and boots it under QEMU with the console on a serial port. The verifier prints
its findings and one marker line, `UTAH_BOOT_OK`, and powers the machine off;
the caller passes only if that marker appears. A VM that panics or hangs cannot
look like a pass, because a hang produces no marker either.

That layer is never published. A published image must contain what it promises
and nothing more, and it certainly must not carry a unit that powers the machine
off once gdm is up.

`post-testing-e2e.yml` runs it for every flavor between verifying the image set
and advancing `:testing`, so an image that does not boot cannot be promoted.
Hosted runners offer no KVM, so this is TCG emulation and takes minutes per
flavor; that is the cost of the only check that can answer the question.

### Image flavors

`config/flavors.json` decides which images exist. Everything derives from it --
the build matrix, the promote matrix, the release matrix, and whether the kernel
cache image below is built at all:

```bash
python3 scripts/flavors.py list          # ["main"]
python3 scripts/flavors.py needs-kernel  # false
```

All four are currently built. A flavor is switched off by removing it from
`flavors` and recording why under `retired`, which keeps the reason beside the
list rather than in a commit message; nothing else has to change, because the
scripts, the Containerfile stages and the cache image all stay put either way.

`just check` fails if any workflow names `utah-nvidia` or `utah-gaming`
directly. Those literals were previously duplicated across three workflows,
which meant narrowing the set in one place left the others promoting and
releasing images the build no longer produced.

### The kernel cache image

`gaming`, `nvidia` and `nvidia-gaming` need an OGC kernel and an NVIDIA kernel
module that no repository ships for this base, so Utah compiles them. That is
about half an hour for the kernel and several minutes for the module, and it
depends on nothing the image build does -- only on the pinned base image and on
`scripts/install-ogc-kernel.sh` and `scripts/install-nvidia.sh`.

So it is paid once, in a separate image built from `Containerfile.kernel` and
tagged with a hash of exactly those inputs:

```bash
just kernel-cache-tag     # the hash
just kernel-cache-ref     # ghcr.io/<owner>/utah-kernel-cache:<hash>
just build-kernel-cache
```

CI builds and pushes it only when that tag is not already published, and the
three flavors that need it use it as their base image; `main` uses the pristine
Hummingbird base and pulls none of it. The install scripts find the archives
under `/utah-cache` and unpack them. With no cache present -- a local
`just build-ghcr`, or the cache image's own build -- the same scripts compile
from source, so there is no second implementation to drift.

Editing either script changes the hash and forces a rebuild, comment-only edits
included. That is deliberate: the key can only ever rebuild something that did
not need it, never reuse something stale.

`main` is submitted to the reusable build workflow separately from the three
kernel flavors (`build_main` and `build_kernel` in `build.yml`), so it starts
the moment the contract check passes and a cache miss delays only the images
that consume the cache. The two calls carry different `brand_name` values
because the reusable workflow keys its own cancel-in-progress concurrency
group on that name; Utah's Justfile ignores it when naming images.

## Where the build time goes

Measured on hosted `ubuntu-24.04` runners, one run, kernel cache hit. The
figures are here so the next person does not have to re-derive them before
deciding what is worth changing.

| Stage | main | nvidia-gaming |
| --- | ---: | ---: |
| Runner setup (podman, btrfs storage) | 2 min | 2 min |
| Pull base and the three source stages | 15 s | 1 min |
| Eighteen one-file COPY layers (before this was fixed) | 3 min 10 s | 3 min 40 s |
| Package transaction | 4 min | 4 min 20 s |
| Extensions, services, branding, contract check | 55 s | 55 s |
| Flavor step (OGC unpack, NVIDIA userspace) | 40 s, a no-op | 3 min 30 s |
| Each further layer commit (shim, clean, lint) | 40 s | 45 s |
| Export, scan, upload (reusable workflow, PR builds) | 3 min | 7 min |
| **Job wall clock** | **17 min** | **24 min** |

Two things follow. A layer commit walks the whole root filesystem, so the
number of instructions in the Containerfile is a cost in its own right, which
is why the sources arrive in as few COPYs as their origins allow and small RUN
steps are folded into their neighbours. And the per-image build arguments
(`VERSION` carries the date and the commit) are declared *after* the package
transaction, because a build argument is part of the cache key of every layer
declared below it: with them at the top, the registry layer cache could never
have hit on the expensive layer.

### Why the image is not sharded across runners

The four flavors already run on four runners; that is the parallel dimension,
and it is exhausted. Within one flavor, every step mutates the same root
filesystem and depends on the one before it -- packages, then the desktop
configured on top of them, then the flavor's kernel work, then cleanup and
lint -- so there is nothing left to hand to a second machine. Building the
shared prefix once and having the flavors start from it was costed and
rejected: pushing and pulling that layer takes about as long as the four
runners take to rebuild it side by side, and pull-request builds cannot push
to GHCR at all. It would reduce compute minutes, which the free tier does not
charge public repositories for, and not wall clock, which it does not help.

The one genuinely serial dependency in the workflow, the kernel cache, is
handled by splitting the matrix (above) rather than by splitting the build.
The kernel compile itself is sixteen of the cache job's twenty minutes on a
four-core runner; the NVIDIA module build that follows it is two and a half,
and only that part could run elsewhere.

### Registry layer cache

`just build-ghcr` passes `--cache-from` for the image's own GHCR package and,
when `REGISTRY_CACHE_WRITE=1` (set by the reusable workflow for non-PR events
only), `--cache-to` as well. An unchanged package transaction is then pulled
rather than rebuilt. Pull-request and local builds read the cache and never
write it. The cache is off, silently, whenever the package is not readable
from where the build runs, which is the case until a testing-branch build has
pushed once.
