---
name: containerfile
version: "1.0"
last_updated: "2026-11-21"
id: containerfile
one_line_purpose: Edit the Containerfile without regressing layer count or cache hits.
entry_point: docs/skills/containerfile.md
category: image-build
mcp_compliance_level: partial
optimization_status: draft
status: active
dependencies: []
tags: [containerfile, layers, caching, build-time]
description: >-
  Layer discipline: COPY folding, late per-image ARGs, script staging,
  clean+lint in one layer. Use when modifying the Containerfile, adding a
  script, or investigating build time.
metadata:
  type: policy
---

# Containerfile

The canonical statement of layer discipline is the "Layer discipline" comment
block at the top of `Containerfile`. It stays there; cite it, do not copy it.
In summary:

- Every instruction commits a layer, and committing a layer walks the whole
  root filesystem to produce the diff -- about ten seconds per layer before
  the package transaction and forty seconds after it, once `/usr` is several
  gigabytes. The eighteen one-file COPYs the build used to open with cost
  three minutes on their own. So sources arrive in as few COPYs as their
  distinct origins allow, and small RUN steps are folded into their
  neighbours.
- The per-image build arguments (`IMAGE_NAME`, `IMAGE_FLAVOR`, `VERSION`,
  `SHA_HEAD_SHORT`, ...) are declared late, immediately before the first step
  that reads them, and the labels that quote them come last. A build argument
  is part of the cache key of every layer declared below it, whether that
  layer uses it or not: with `VERSION` at the top, the package transaction
  missed the registry layer cache on every commit, since `VERSION` carries
  the date and the commit. Nothing above the branding step ever sees them.
- The package repository is bind mounted, never copied:
  `RUN --mount=type=bind,from=packages,source=/repository,target=/etc/utah-packages,ro`
  on the two steps that install from it. A `COPY --from=packages` used to put
  the whole 4 GB repository into the image, nothing removed it, and it was two
  thirds of every published image and of every ISO (#130). `just check`
  refuses a `COPY --from=packages`. `packages/utah-packages.repo` is committed
  with `enabled=1`, because the two mounted steps install from it; the flavor
  step, the last one that installs anything, flips it to `enabled=0` so a later
  dnf call on the finished image -- the live ISO build among them -- does not
  fail on a baseurl that is no longer mounted (comment, the repo file itself).
  The flip also appends a comment line so the file's size changes; see
  "Same-size edits after an mtime pin" below for why (#735).
- `ARG PACKAGE_IMAGE_REF=${PACKAGE_IMAGE}@${PACKAGE_IMAGE_SHA}` defaults to
  the digest-pinned OCI package repository for reproducible CI builds, while
  allowing local composition to inject a local image from containers-storage
  via `just build-local`.
- A `PACKAGE_IMAGE_SHA` bump must also move the `# factory-pin:` stamp in
  `packages/utah-packages.repo`. The transaction reads the `packages` stage
  through a bind mount, which is not part of the RUN cache key, and the ARG
  change alone does not bust the layer on CI's buildah -- a pin-only commit
  rebuilt nothing and shipped the previous factory's packages (#371). The
  stamp rides a COPY before the transaction, and COPY content always keys the
  cache. A test fails the build when the two disagree.
- Renovate's built-in Dockerfile extraction skips the composed package ARG.
  The repository's regex manager instead discovers the digest directly in
  `PACKAGE_IMAGE_SHA` and in the `.repo` stamp, with both occurrences grouped
  as `ghcr.io/projectbluefin/utah-packages:latest`. This retains the local
  `PACKAGE_IMAGE_REF` override and the digest-only OCI factory label.
  Renovate 44.132.2 extraction and real replacement were exercised against
  both files: they change to one digest while preserving unrelated content.
  The duplicate scheduled updater is retired; the grouped PR must still
  pass the package transaction and cache-stamp equality checks.
- External executable release assets (such as `uupd`) are pinned by version
  and verified with explicit sha256 checksums (`UUPD_SHA256`) before
  extraction. `UUPD_VERSION` follows Dakota's `elements/bluefin/uupd.bst`;
  the unit digests (`UUPD_SERVICE_SHA256`, `UUPD_TIMER_SHA256`) cover the
  raw files at that tag and may be unchanged across releases (v1.4.0 and
  v1.5.5 serve identical units).
- `COMMON_IMAGE_SHA` follows Common's latest published image: Renovate bumps
  the digest, and each bump passes the desktop contract before it lands.
  Dakota instead builds Common at a release tag (`v2026.11`, `07d6b3e`). Utah
  keeps tracking the digest on purpose (#1267): Common fixes Utah depends on,
  such as the PipeWire drop-in that now parses, would otherwise wait for a
  Common release. Compare against Dakota's tag only when a Common file differs
  between the two images. Drop a Utah override of a Common file once the
  pinned Common ships the fix itself.
- Artwork reaches the image through the throwaway `artwork` stage, which keeps
  the fetch tooling and unprocessed sources out of every layer.
  `WALLPAPERS_IMAGE`/`WALLPAPERS_IMAGE_SHA` pin the multi-arch index digest of
  `ghcr.io/projectbluefin/bluefin-wallpapers-gnome`; Renovate bumps the one ARG
  line exactly as it does `COMMON_IMAGE_SHA`. The stage applies Common's own
  normalization to that image (flat `*.jxl`/`*.svg`/`*.xml` into
  `/usr/share/backgrounds/bluefin`, `~/.local/share` rewritten to
  `/usr/share` in the XML), then fails if any path an XML names is missing.
  Its `/out/overlay` is copied after Common's files in the first RUN, so the
  direct pin **wins** over Common's transitively pinned copy; files only Common
  has (`bluefin-hidamari.webm`) are left alone. `/out/branding` is bind
  mounted into the branding RUN, which rasterizes the GDM logo.
  Everything else comes from `config/artwork.lock` through
  `scripts/fetch-artwork.sh` (one commit, one sha256 per path, fails closed).
  Renovate does not touch the lock, because it cannot recompute checksums:
  bump the commit and the checksums together by hand.
- The `artwork` stage runs on `ARTWORK_BUILDER_IMAGE`, a Red Hat Hardened
  Images builder (`registry.access.redhat.com/hi/core-runtime:latest-builder`,
  `tag@sha256` of the multi-arch index), not on `BASE_IMAGE`: it only needs a
  shell, curl and coreutils, and off the base it no longer rebuilds with every
  kernel cache or base bump. The image runs as UID 65532 and ships no awk, so
  the stage sets `USER 0` and installs `gawk` with the image's dnf before
  `fetch-artwork.sh` runs. The contract job verifies every `/hi` pin's
  signature against Red Hat's key in `config/redhat-hi-cosign.pub` (`just
  verify-rhhi-signatures`); Renovate's Dockerfile manager bumps the ARG,
  grouped and weekly (`renovate.json`). Use `/hi` (signed), never the unsigned
  `quay.io/hummingbird/` mirror. Do not move a stage onto an RHHI image unless
  the image provides what the stage runs: check for a shell, dnf, the user,
  and each tool. The shared pinned-fetches gate exempts only
  `:latest@sha256:`, so a new `:latest-builder@sha256:` pin also needs a line
  in `.github/pinned-fetches-allowlist.txt`.
- Images kept off RHHI, and why: `BASE_IMAGE` (`os-minimal` is a
  hummingbird-community bootc image, not in `/hi`) and the `v4l2loopback`
  stage, which compiles against the base's kernel; `iso/raw/Containerfile`'s
  Fedora builder, because the Hummingbird repositories lack
  `blueprint-compiler` and the GTK4/libadwaita/gnome-desktop4/libgweather4/vte
  `-devel` packages OS-Installer builds against; `ghcr.io/qemus/qemu`,
  `easy-novnc` and tacklebox (no RHHI equivalent); and every image that runs
  Utah itself (`check-repos`, `check-desktop-contract`, the ISO scripts).
- The digest-pinned `packages` stage is the package factory's RPM repository,
  and it is **bind mounted, never copied**: `RUN --mount=type=bind,
  from=packages,source=/repository,target=/etc/utah-packages,ro` feeds it to the
  two RUN steps that install from it. A `COPY --from=packages /repository
  /etc/utah-packages` committed the whole ~4 GB repository to a layer that
  nothing removed -- two thirds of every published image and of every live ISO
  (#128). The mount costs no layer. The flavor step flips `utah-packages.repo`
  to `enabled=0` last, so later dnf calls on the image (the live ISO build's
  included) do not fail on a `file://` baseurl that is no longer present. `just
  check` asserts the mount and refuses a `COPY --from=packages`.

## Clock synchronization and TLS failures

Time sync is `systemd-timesyncd`, as on GNOME OS and Dakota (#1228): chrony
is in `packages/base.toml` `[excluded]`, `85-utah-desktop.preset` enables
timesyncd over Hummingbird's `90-default.preset` disable, and
`/usr/lib/systemd/timesyncd.conf.d/10-gnome-os-fallback-ntp.conf` sets
`FallbackNTP=gnome.pool.ntp.org`, the server gnome-build-meta compiles in
(`core-deps/systemd-base.bst`). Hummingbird's compiled-in
`*.hummingbird.pool.ntp.org` does not resolve publicly, and NetworkManager
does not pass DHCP NTP servers to timesyncd, so without the drop-in a fresh VM
clock can sit behind certificate validity dates (#594). Before treating a TLS
"not yet valid" failure as missing CA certificates, check
`timedatectl timesync-status` on the booted machine. Dakota's
`ublue-firstboot-date.path` starts the install-date stamp only once
`/run/systemd/timesync/synchronized` exists. A source-string assertion alone
cannot prove time sync; confirm it on a booted VM.

## Where the build time goes

Measured on hosted `ubuntu-24.04` runners, one run, kernel cache hit. The
figures are here so the next person does not have to re-derive them before
deciding what is worth changing.

| Stage | main | nvidia-gaming |
| --- | ---: | ---: |
| Runner setup (podman, btrfs storage) | 2 min | 2 min |
| Pull base and the four source stages | 15 s | 1 min |
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

## Why the image is not sharded across runners

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
handled by splitting the matrix (see `docs/skills/flavors.md`) rather than by
splitting the build. The kernel compile itself is sixteen of the cache job's
twenty minutes on a four-core runner; the NVIDIA module build that follows it
is two and a half, and only that part could run elsewhere.

## Registry layer cache

`just build-ghcr` passes `--cache-from` for the image's own GHCR package and,
when `REGISTRY_CACHE_WRITE=1` (set by the reusable workflow for non-PR events
only), `--cache-to` as well. An unchanged package transaction is then pulled
rather than rebuilt. Pull-request and local builds read the cache and never
write it. The cache is off, silently, whenever the package is not readable
from where the build runs, which is the case until a testing-branch build has
pushed once.

The transaction's cache key is its COPY'd inputs: the manifests, the repo
files (the `# factory-pin:` stamp among them, #371) and the install script.
Hummingbird's own repository is unpinned and rolling, so none of those move
when it publishes, and the cache used to replay the same transaction until a
base-image bump busted it. `build-ghcr` therefore resolves the repository's
`repomd.xml` `<revision>` -- a publish timestamp -- and passes its UTC day as
`ARG HUMMINGBIRD_REPO_DAY`, declared directly above the transaction. The day,
not the raw revision: Hummingbird republishes several times a day, and keying
on every publish would rebuild the most expensive layer on nearly every run.
Unresolvable metadata warns and builds with `unresolved`; local builds keep
the `unset` default.

## Adding a script

All of Utah's scripts arrive in one COPY, staged under `/tmp/utah-scripts/`
because a multi-source COPY cannot rename, and installed by name into
`/usr/local/libexec/` by the rename loop in the same RUN (comment and loop,
`Containerfile`). The checklist for a new script:

1. Add the file to the `COPY scripts/... /tmp/utah-scripts/` list.
2. Add a `source:utah-<name>` pair to the rename loop so it lands at
   `/usr/local/libexec/utah-<name>` -- every downstream path expects the
   `utah-` prefix.
3. Run `just check`.

The destination directory may be absent in the Hummingbird base. Use
`install -Dm 0755` in the loop, retaining `${pair%%:*}` for the source and
`${pair##*:}` for the destination. `${pair##:*}` does not strip the source
name: it installs a filename containing the entire colon-separated pair.

## Build-only dependencies go in a builder stage

When a step needs a package the image must not ship, compile it in a stage of
its own and hand over only the result. The `v4l2loopback` stage is the example
(#291): it installs kernel-devel (from Koji, signature-checked against the
committed `packages/RPM-GPG-KEY-fedora-44-primary`) plus Fedora 44 to resolve
its build dependencies, compiles the module and `v4l2loopback-ctl` into
`/out`, and the final stage bind mounts `/out` into the script-staging RUN
(`RUN --mount=type=bind,from=v4l2loopback,...`) instead of COPYing it, so no
layer is added. The builder never runs `depmod` against `/out`: a partial
`modules.dep` would be laid over the image's. `utah-install-v4l2loopback base`
runs again in the flavor step, finds the staged module, and only registers and
asserts it; `utah-install-v4l2loopback ogc` compiles against the OGC tree that
`install-ogc-kernel.sh` preserves, which needs `CONFIG_VIDEO_DEV` in that
kernel (enforced by its `required_config`). That `ogc` run is the tree's last
consumer, so once its module is verified it removes `/usr/src/linux-<ogc>` and
the `modules/<ogc>/build` symlink (#684). The tree is 286 MiB uncompressed --
`cp -a arch/x86` carries the built objects and boot images -- and cost about
150 MiB of the live ISO's zstd-3 squashfs on every gaming flavor. The removal
lives in `install-v4l2loopback.sh` and not in `install-ogc-kernel.sh` for two
reasons: that script is in the kernel cache tag, and the cache tarball still
has to carry the tree for the NVIDIA and v4l2loopback compiles; and it lands
in the same RUN layer that unpacked the tree, where a deletion actually
shrinks the layer. A later layer's whiteout only helps after chunkah
rechunks, and a local `just iso` embeds the un-rechunked layers as they are.

After the final NVIDIA, OGC, and v4l2loopback package steps, the flavor layer
runs `utah-regenerate-initramfs`. The helper selects the exact bootc kernel,
promotes the OGC kernel for gaming flavors, rebuilds that release's initramfs,
and verifies early microcode plus the installed TPM/passkey/smart-card unlock
modules, the Plymouth theme, and (where the kernel builds it as a module) the
zstd driver `zswap.compressor=zstd` needs at init (#1224), before the image
can commit.

## Clean and lint share a layer

Everything above writes build-time residue that bootc lint rejects: dnf logs
under `/var/log`, cockpit and dnf state under `/run`, ~45 `/var`
directories with no tmpfiles.d entry, and kernel files under `/boot`.
`utah-clean-stage` must run after the last package install, which is the
NVIDIA and OGC step, not after the main transaction. The lint that checks the
result runs in the same layer (`bootc container lint --fatal-warnings`, every
check enabled): nothing can change between the two (comment, `Containerfile`).

`/boot` ships empty. bootc deploys the kernel and initramfs from
`/usr/lib/modules/<kver>/`; the image's `/boot` is masked by the ostree
backend but not by the composefs backend, whose composefs-boot reads
`/boot/loader/entries/*.conf` as Type #1 boot entries. bootc then refuses a
BLS install with `Setting up BLS boot: Found Type1 entries in /boot` (#1129).
The entry came from the kernel cache: `install-fedora-kernel.sh install`
runs `rpm -U`, kernel-core's `%posttrans` runs `kernel-install add`, and
grub2-common's `20-grub.install` ignores `layout=ostree` and writes
`/boot/loader/entries/<machine-id>-<kver>.conf` (options taken from the build
runner's `/proc/cmdline`) plus copies of vmlinuz, initramfs, System.map,
config and symvers. The OGC installer adds `/boot/vmlinuz-<release>`, which
`regenerate-initramfs.sh` has already hardlinked into `/usr/lib/modules`.
`utah-clean-stage` empties `/boot`, and the lint's `nonempty-boot` check (it
was skipped from the first commit until #1129) fails the build on anything
left. To check a published image: `podman run --rm --network=none <image>
find /boot` must print only `/boot`.

The one deliberate exception is `/var/home`, created after clean-stage but
before lint in that same RUN. useradd ships `HOME=/home` (#576) and
`home.mount` binds `/var/home` over `/home` (Dakota's bind mount, #1234), so
the homes live in `/var/home`: the installer pre-creates it in the
stateroot, and the tacklebox customize container and live ISO build create
liveuser's home there (#602). clean-stage strips all of `/var` except cache,
so the mkdir cannot go earlier; placing it before lint keeps lint proving
the directory is covered by the `utah-home.conf` tmpfiles entry.

The same RUN replaces Hummingbird's `/home -> var/home` symlink with an
empty directory, the bind-mount point, and fails if anything is in it:
`home.mount` hides it. A build has no mounts, so build-time code that
creates a home must write `/var/home/<user>`, not `/home/<user>`.

Installer note: fisherman's ostree path runs `chroot <deployment> useradd
--create-home`, which now writes a skel copy into the deployment's own
`/home`, hidden at boot. The account still gets its home: fisherman also
writes a tmpfiles `C /var/home/<user> - /etc/skel` rule that the first boot
applies. Anything else an installer writes into `/home/<user>` before the
first boot is lost the same way.

The image also ships an empty `/efi` directory: GRUB can report the EFI system
partition to systemd, whose generator selects `/efi` when `/boot` is occupied.
An immutable root cannot create that mountpoint on boot. The installed-system
ISO test checks the directory and that `efi.automount` has not failed; a GRUB
`bootc install to-disk` VM should also be checked when changing this behavior.

## `just` override and the 1.56 floor

Utah's `00-entry.just` imports Common's renamed entry (`00-common.just`) plus
its own `60-custom.just` at a shallower depth than Common's own `import?`
lines reach `60-custom.just`. The override wins on `just` >= 1.56, which
stopped deduplicating an AST across nested imports of the same file; earlier
versions deduplicated, Common's deeper import shadowed ours, and every
override silently reverted to Common's recipe (issue #449). The Containerfile
preserves the mechanism by renaming Common's `00-entry.just` to
`00-common.just` before staging Utah's local files, so the shallower override
is in place by the time the entry point runs.

Utah's local files also replace Common's `default.just`, `system.just` and
`flutter.just` with Dakota's (`files/just-overrides/` at `bf271777`, #1231),
plus the two Brewfiles `system.just` reads from
`/usr/share/ublue-os/homebrew/`. Common's entry still imports them by name;
`changelog.just`, `update.just` and the other Common files stay Common's. The
headers list Utah's edits: issue links and `rebase-helper` target Utah, and
recipes that need Dakota's signed systemd-boot chain (#1225) or nethogs mount
exit with an explanation. Overrides of those recipes go in `60-custom.just`;
keep the vendored files close to Dakota so they can be re-synced.
`tests/test_ujust_overrides.py` loads the shipped files, so a recipe that
breaks parsing or loses an override fails there.

The shipped image is already past the floor: `baselines/utah/rpms.tsv` records
`just 1.57.0-1.hum1.bfin`. The `just` package comes from the base manifest and
its version is not pinned here. Two checks keep it that way:
`tests/test_ujust_overrides.py` asserts the baseline NEVR stays >= 1.56 so an
image regression below the floor fails the suite, and the same module's
host-side override tests skip with a message naming issue #449 when the
developer's own `just` is below the floor. `just` is listed in
`packages/base.toml` -- do not pin or override its version there or in
`packages/utah.toml`.

## Verification

```bash
just check
grep -c '^COPY\|^RUN' Containerfile
```

The count stays at its current value (22 as of 2026-10-24: 14 for the shipped
image, plus the four `v4l2loopback` and four `artwork` builder-stage
instructions, which never reach it)
unless the change justifies a new layer against the timings table above.
Removing the package repository COPY dropped it from 13 to 12; a later change
must earn its layer.

### Native layer reproducibility

Final cleanup does not repair earlier native layers. Stage COPY origins in a
scratch input stage and bind it into the existing overlay RUN; normalize
generated metadata before each runtime layer commits. Preserve unchanged RPM
payload mtimes, symlink targets, and read-only mounts. Clear transaction logs,
SQLite state, machine-id and random seed in their producing layer.

Deletion-marker headers are invisible in the merged filesystem. Canonical
Just recipes use Podman >= 5.5 timestamp rewriting with an epoch derived from
the latest commit to build input paths; documentation-only commits retain the
cache key. Before export, reject an epoch older than any RPM payload so Python
timestamp bytecode is never invalidated. Keep the final helper on a read-only
mount outside /tmp, because cleanup removes installed helpers and clears /tmp.

Run `just check-reproducible main` to compare two uncached builds with fixed
arguments and every ordered native layer. The combined review candidate at
e44e438 matched all 37 layers and image IDs in run 37576445666. Published
chunkah ordering and non-main flavor acceptance need separate evidence.

### Same-size edits after an mtime pin

A file rewritten in a later layer must differ from the earlier layer's copy
in size or mtime, not only in content. Every runtime layer ends with
`utah-pin-build-mtimes`, which stamps each path RPM does not own with
`SOURCE_DATE_EPOCH` -- the same value the earlier layer's copy already
carries. containers/storage's naive layer diff, which buildah uses on kernels
with `CONFIG_OVERLAY_FS_REDIRECT_DIR` (the hosted runners among them),
decides "modified" from mode, owner, mtime and size alone and never reads
content. An in-place edit that keeps the byte count is therefore dropped from
the layer, and the merged image -- and so chunkah's rechunked output -- keeps
the old bytes while the RUN's own assertions passed.

That is how #735 shipped: the flavor step's `sed` from `enabled=1` to
`enabled=0` in `utah-packages.repo` keeps the length, the pin followed it, and
the published image carried the repository enabled with a `file://` baseurl
that exists only during the build; the live ISO build's first `dnf5 install`
failed on it. The flip now appends a comment line to the file and greps for
it, so the diff cannot miss the change and the build fails closed if the
added bytes go missing (`tests/test_package_resolution.py`). Apply the same
rule to any future `sed -i` on a file an earlier layer already committed:
add or remove bytes, or do the edit in the layer that first writes the file.

### Image provenance

Canonical build recipes pass the full Utah commit and extension provenance
through late build arguments. `verify-gnome-extensions.py --print-provenance`
resolves each installed UUID to `.gitmodules` and the index gitlink, rejecting
mismatched or modified checkouts. Resolve this in a standalone shell assignment
before invoking the build engine, so `set -e` stops on a failed pin check;
command substitutions inside build arguments do not propagate their failure.
Two Bluefin Bling extensions share one pin.
Release-archive extensions record the verified download URL and SHA256 as
well as their reference source commit; that commit does not identify the
archive's built bytes.

After extension installation, `--record-provenance` writes
`/usr/share/utah/provenance.json`, including installed metadata versions and
the image references actually passed to the build. For kernel flavors,
`build-ghcr` and `check-reproducible-build.sh` replace `BASE_IMAGE` with the
cosign-verified kernel cache, so `base_image` identifies `utah-kernel-cache`,
not the Hummingbird pin declared at the top of the Containerfile. The declared
Hummingbird pin is not recorded separately in this file; inspect
`Containerfile.kernel` at the recorded Utah commit (`utah.commit`) to find it.
The `main` flavor uses the declared Hummingbird base unless explicitly
overridden. Local unpinned image references have a null digest rather than
borrowing the production pin.
The `built` timestamp uses `SOURCE_DATE_EPOCH` (the reproducible build-input
time), not wall-clock time. Keep this in the existing desktop RUN before
mtime normalization. Direct Containerfile callers must provide `SHA_HEAD`
and `GNOME_EXTENSION_PROVENANCE`; missing provenance fails the build.
