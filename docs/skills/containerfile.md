---
name: containerfile
version: "1.0"
last_updated: "2026-10-03"
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
  extraction.
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
kernel (enforced by its `required_config`).

## The initramfs is regenerated after the last package install

The base image ships an initramfs built before Utah installs anything, and
bootc boots it as-is. Left alone it carried no early CPU microcode and none of
Common's TPM/passkey unlock modules (`90-passkeys-tpm.conf`), which went
unnoticed because the image booted fine (#564). `utah-regenerate-initramfs`
runs at the end of the NVIDIA and OGC step, after `utah-verify-rpm-contract`:
it rebuilds the initramfs of the one kernel with a `vmlinuz` under
`/usr/lib/modules` (`dracut --no-hostonly --reproducible`; everything else
comes from `dracut.conf.d`) and fails the build unless `lsinitrd` shows the
early microcode cpio with both vendors' blobs and the `bootc ostree fido2
tpm2-tss pkcs11 pcsc` modules.

dracut refuses a module whose binaries are missing instead of skipping it, so
a dracut.conf.d file from Common that names a new module means a package in
`utah.toml` too: `tpm2-tss` needs `tpm2` (tpm2-tools) and `pcsc` needs
`pcscd` (pcsc-lite). Check with
`dracut --no-hostonly -f /var/tmp/t.img <kver>` on a booted VM.

## Clean and lint share a layer

Everything above writes build-time residue that bootc lint rejects: dnf logs
under `/var/log`, cockpit and dnf state under `/run`, and ~45 `/var`
directories with no tmpfiles.d entry. `utah-clean-stage` must run after the
last package install, which is the NVIDIA and OGC step, not after the main
transaction. The lint that checks the result runs in the same layer
(`bootc container lint --fatal-warnings --skip nonempty-boot`): nothing can
change between the two (comment, `Containerfile`).

The same step removes final-rootfs build residue. It drops the dnf5 transaction
history -- `usr/lib/sysimage/libdnf5/transaction_history.sqlite` and its
`-shm`/`-wal` companions -- build-time metadata nothing reads at runtime, but
it carries a wall-clock mtime that churns its layer on every rebuild. It then
pins the mtimes the build itself wrote under `/usr`, `/etc`, `/var` and `/boot`
to `SOURCE_DATE_EPOCH`: chunkah splits those directories across layers, so any
wall-clock mtime in a tar header changes that layer's digest. The pin also covers
the directories the script rewrites itself -- `/`, `/var` (recursively, so the
surviving `/var/cache/rpm-ostree` is included), `/run` and `/tmp` -- because
removing an entry stamps the wall clock on the parent directory, and those
entries ship in a layer too. A rebuild that changes nothing must produce an
identical image (utah#313). This normalization lands in `utah-clean-stage`, the
final layer, because chunkah reads the merged rootfs -- a touch there is the
last write, so it wins over the wall-clock mtimes the package and extension
steps left. The `touch` must pass `-h`: the tree carries symlinks whose target
is not in the image (`/usr/lib/bootc/storage`, the malcontent `COPYING` links,
the 32-bit `libstdc++.a` stubs), and a dereferencing `touch` exits non-zero on
each one and fails the layer under `set -e`. `-h` stamps the link itself, which
is the mtime the tar header carries anyway.
It is not a blanket `touch` of `/usr`, and must not become one. A path RPM
installed and the build never rewrote already carries a reproducible mtime --
the one from the package payload, fixed by the pinned package image -- and
re-stamping it breaks two things that read it. Fedora byte-compiles with
`--invalidation-mode=timestamp` (`brp-python-bytecompile`), so each `.pyc`
records the mtime its `.py` had: move the source without rewriting the `.pyc`
and every stdlib import recompiles in memory. And `rpm -V` compares the same
mtime, so it reports `T` for every file in the image. Neither shows on a booted
bootc host -- ostree deploys with mtime 0, so the check is already lost there --
but the ISO compose, CI and `podman run` all read the image as a container,
where the stamp survives. So `clean-stage.sh` asks RPM for the mtime it gave
each path (`rpm --root -qa --qf '[%{FILENAMES}\\t%{FILEMTIMES}\\n]'`) and pins
only the mismatches: what RPM does not own (everything COPYed in, the GNOME
extensions meson installs, the compiled schemas) and what the build rewrote
after RPM wrote it (`ld.so.cache`, `sed -i` targets, every directory dnf wrote
into). With no readable rpmdb -- a scratch tree in the unit tests -- the
sweep falls back to pinning everything and says so on stderr.
Canonical recipes derive the epoch from the latest Git commit affecting build
input paths (`Containerfile`, packages, system_files, scripts, config, and
.gitmodules). Documentation-only commits retain the cache key. The caller may
supply a fixed SOURCE_DATE_EPOCH; guarded exports reject it if any RPM payload
time is newer. The Containerfile and standalone cleanup retain their legacy
fallback for direct invocations without canonical recipe arguments.
That pin re-stamps every `/usr/share/fonts` directory dnf wrote into, which
invalidates the system font caches in the layers a container runs from, so the
pin loop is followed by `fc-cache --sysroot="$CLEAN_ROOT" --force --system-only`
with `SOURCE_DATE_EPOCH` exported. fontconfig accepts a cache under
`/usr/lib/fontconfig/cache` only when its stored checksum equals the font
directory's current mtime, and Fedora's `%transfiletriggerin` built those caches
from the wall-clock mtimes dnf wrote. The rebuild removes the trigger caches'
embedded wall-clock checksums for reproducibility and gives container readers
(ISO compose, CI, `podman run`) caches matching the final font-directory mtimes.
It must run after the pin, so the checksum records the final mtime, and its own
output must then be re-pinned -- `fc-cache` writes with the wall clock, so
leaving it would churn that layer. Use `fc-cache-64` directly when Fedora
provides it: its `fc-cache` wrapper swallows architecture-specific failures,
which must instead fail composition. `FC_CACHE` selects an explicit executable
for isolated scratch-tree tests.
The same principle applies at the source: `build-gnome-extensions.sh` removes
GSConnect's `_build/` after `meson install`, exactly as it already removes
Blur My Shell's `build/`, so the timestamped artifact never reaches the image
to be normalized downstream. Prefer dropping such a directory where it is made
over re-touching it in `utah-clean-stage`.
The acceptance test for all of this is an outcome, not a unit test:
`just check-reproducible [flavor]` builds the flavor twice, uncached, with the
wall-clock build args held constant, and diffs the ordered layer digests
(`scripts/check-reproducible-build.sh`). `--no-cache` is the point -- a second
`podman build` of an unchanged Containerfile otherwise replays the layer cache
and passes whatever the build scripts leave behind. Two full builds, so it is
not in `just check` or the PR matrix; run it when changing anything that writes
into the image. No regular PR gate runs it, so a change claiming reproducibility should
cite its own run. An isolated review branch can dispatch the read-only review
reproducibility job with `contract_only` and `review_repro` true; retain its
source SHA, both inspections and comparison log. This establishes native
layer behavior, while published rechunking still needs separate evidence. The comparator checks every ordered native layer; do not squash or ignore layers to claim a passing
result.

### Timestamp discipline starts before the transaction

RPM 6's `rpmtsCreate`/`rpmtsGetTime` honor `SOURCE_DATE_EPOCH`, so the final
stage declares the fixed epoch as an `ARG` before any transaction (every `RUN`
sees it; not `ENV`, which would persist in the published image config and
leak into `iso/live`, `podman run` and downstream builds): the final RPM database
would otherwise still differ in the `INSTALLTIME`/`INSTALLTID` header fields,
not merely its file mtime. The build recipes pass `--source-date-epoch` to
Podman (guarded on Podman >= 5.5 by `scripts/podman-epoch-args.sh`, warn-and-continue below it), which fixes the
image-created metadata. Canonical exports also use `--rewrite-timestamp` with
the build-input epoch to normalize generated deletion-marker headers, which
are invisible in the merged filesystem. `UTAH_REWRITE_TIMESTAMPS=1` requires
the per-layer helper to reject any RPM payload mtime newer than that epoch
before export. An unguarded 2024 epoch would clamp packaged Python sources
and invalidate their timestamp bytecode; never drop the guard or blindly
rewrite all timestamps. Published chunkah ordering still needs its own proof.
Timestamp plumbing does not cover files whose bytes, not mtimes, carry
wall-clock or run-specific state: dnf5 logs, transaction-history SQLite WAL/SHM
state, and the regenerated ibus, ldconfig and swcatalog caches. Those files are
removed at both producing RUN boundaries, before their bytes can reach a native layer; deleting them only in
final cleanup leaves the earlier blobs nondeterministic. This targeted removal
does not sweep `/tmp`, the generic-logo RPM or `/utah-cache`, which later
stages still need.

The one deliberate exception is `/var/home`, created after clean-stage but
before lint in that same RUN. `/home` is a symlink to `var/home` and
useradd ships `HOME=/home` (#576), so any `useradd --create-home` fails on
a dangling symlink -- the installer chroot on a fresh install (no tmpfiles
has run there yet), the tacklebox customize container, and the live ISO
build all broke with `cannot create directory /home`, exit 12 (#602).
clean-stage strips all of `/var` except cache, so the mkdir cannot go
earlier; placing it before lint keeps lint proving the directory is covered
by the `utah-home.conf` tmpfiles entry. A tmpfiles `d` line alone is not
enough -- it only runs at boot, never in the installer chroot.

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

The shipped image is already past the floor: `baselines/utah/rpms.tsv` records
`just 1.57.0-1.hum1.bfin` (Bluefin's parity manifest, `baselines/bluefin/rpms.tsv`,
records `1.57.0-1.fc44`). The `just` package is inherited from Bluefin and its
version is not pinned here. Two checks keep it that way:
`tests/test_ujust_overrides.py` asserts the baseline NEVR stays >= 1.56 so an
image regression below the floor fails the suite, and the same module's
host-side override tests skip with a message naming issue #449 when the
developer's own `just` is below the floor. `just` is already listed in
`packages/bluefin.toml` as part of the mirrored parity manifest -- do not pin
or override its version there or in `packages/utah.toml`; that contract
belongs to Bluefin.

## Verification

```bash
just check
grep -c '^COPY\|^RUN' Containerfile
```

The count stays at its current value (17 as of 2026-09-26: 13 for the shipped
image, plus the four `v4l2loopback` builder-stage instructions, which never
reach it)
unless the change justifies a new layer against the timings table above.
Removing the package repository COPY dropped it from 13 to 12; a later change
must earn its layer.

When the two-build gate fails, final-rootfs cleanup cannot establish native
layer equality: earlier committed layers retain their original metadata.
Capture the first differing overlay layer's path metadata, xattrs, and content
hashes in the disposable runner before it exits; retain that diagnostic with
the two image manifests. A successful image build alone proves neither native
layer nor rechunked publication reproducibility.

The real two-build probe found COPY destination-parent directory mtimes in the
first differing native layer. Stage COPY inputs in a scratch origin and bind
that origin into the first runtime RUN; normalize generated mtimes before
each runtime layer commits, preserving mtimes that match RPM's payload index.
Skip bind mounts so normalization cannot alter read-only package repositories
or build inputs. Final cleanup alone cannot repair earlier committed layers.

The final normalizer must run from a read-only build-tools mount outside `/tmp`: cleanup removes installed Utah helpers and empties `/tmp` before lint. A helper installed earlier cannot be called after that sweep.

Native deletion markers are invisible in the merged filesystem: the diagnostic
found 175 whiteouts with wall-clock metadata, plus random machine-id/seed bytes
in the RPM layer. Clear generated identities in that producing layer. The
experimental native probe uses a source-commit epoch with tar timestamp rewriting
and rejects an epoch older than any RPM payload before export. This guard is
needed to preserve RPM/Python timestamp semantics. Canonical build recipes use the guarded policy; chunkah acceptance still
requires its own proof.

Two uncached native builds at e44e438 matched all 37 layers (run 37576445666).
The production Just recipes now use the same guarded export policy. Derive the
default epoch from commits affecting build input paths, rather than every Git
commit, so documentation-only changes do not invalidate transaction caches.
If a rolling RPM has a newer payload time, fail closed and supply an appropriate
fixed SOURCE_DATE_EPOCH; do not clamp packaged Python source timestamps.
