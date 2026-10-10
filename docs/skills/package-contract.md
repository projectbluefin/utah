---
name: package-contract
version: "1.1"
last_updated: "2026-10-03"
id: package-contract
one_line_purpose: Maintain Bluefin package parity and Utah's overlay manifest.
entry_point: docs/skills/package-contract.md
category: contracts
mcp_compliance_level: partial
optimization_status: draft
status: active
dependencies: []
tags: [packages, parity, bluefin, contracts]
description: >-
  Bluefin parity contract: verbatim bluefin.toml, utah.toml overlay, device
  firmware, [unavailable] rules, repository policy (on-image reposdir
  scan). Use when adding, removing,
  or debugging packages or parity/check-repos failures.
metadata:
  type: policy
---

# Package Contract

Utah keeps Bluefin's user-facing package contract on a Hummingbird base. Two
manifests under `packages/` define what the image installs; this skill is the
policy for changing them.

## The two manifests

- **`packages/bluefin.toml`** — the parity contract. It is a byte-for-byte
  copy of projectbluefin/bluefin's `build_files/packages/base.toml` pinned to
  the revision in `packages/.bluefin-parity-ref` and must stay that way.
  **Never hand-edit it.** Sync it verbatim from upstream; any drift is a parity
  bug. `just check-parity` diffs it against upstream on every CI run so drift
  fails the build rather than accumulating quietly (recipe comment: `Justfile`,
  `check-parity`).
- **`packages/utah.toml`** — Utah's overlay. Everything Utah needs *in
  addition to* or *instead of* the contract lives here. The full rules are in
  the header comment of that file (cite it; do not move or copy it):

  - `[gnome]` — GNOME 51 desktop contract Hummingbird does not ship.
  - `[build]` — toolchain needed to build the pinned GNOME extensions
    (`scripts/build-gnome-extensions.sh`).
  - `[parity]` — what Bluefin inherits from Fedora's base image and Hummingbird
    has in its repository but not in its bootable base; CI's package
    availability step resolves the real transaction and is the gate on every
    name there.
  - `[hardware]` — device firmware the bootable base leaves out entirely. Its
    own section rather than `[parity]`, because it is parity with nothing:
    Hummingbird's repository has no `linux-firmware` to inherit, the factory
    builds it, and nothing in the image `Requires` it. `linux-firmware` is a
    split package whose thirteen Recommends omit Intel wireless, so the iwlwifi
    and iwlegacy packages are named explicitly — the X230's
    `iwlwifi-6000g2a-6.ucode` ships in `iwlwifi-dvm-firmware` (#97).
  - `[services]` — desktop services Bluefin adds on top of the server base.
  - `[unavailable]` — Bluefin parity gaps none of Utah's enabled repositories
    provide, whether the name comes from the copied `base.toml` contract or
    from the published Bluefin image snapshot
    (`baselines/bluefin/rpms.tsv`).

## Wi-Fi documentation currency

Wi-Fi needs both `[hardware]` firmware and the `[parity]` userspace stack:
`NetworkManager-wifi`, `wpa_supplicant`, `wireless-regdb` and `iw`. These names
are now in the install contract; the former factory dependency blocker is
not a current gap. When a dependency lands, update both the nearby manifest
comments and the README gap list. Keep package availability separate from
runtime evidence: only testing device detection and association on the target
hardware establishes that its radio works.

## [unavailable] rules

`[unavailable]` means "no repository Utah enables provides this name at all".
It covers both kinds of parity gap: names in the copied `base.toml` contract,
and names Bluefin's published image ships from a build file outside that
contract (recorded in `baselines/bluefin/rpms.tsv` and triaged in
`baselines/triage.toml` — `nss-mdns` is the current example). Each entry
**MUST carry a tracking issue**: the list is the documented parity debt, not
a dumping ground for packages that are merely inconvenient (header comment,
`packages/utah.toml`).

## Media packages and codec parity

Bluefin's `[multimedia_overrides]` selects replacement builds from negativo17;
Utah does not enable that repository or consume that section wholesale.
Do not infer that Hummingbird installs a name just because it appears there.
The Intel VA-API driver (`libva-intel-media-driver`, providing
`iHD_drv_video.so`) and `intel-gmmlib` must be requested in Utah's `[parity]`.
It also requests `intel-mediasdk` and `intel-vpl-gpu-rt` for the two Intel
runtime generations (#383). Those four are the only `[multimedia_overrides]`
names Utah requests; the other eight (`libheif`, `libva`, and the six `mesa-*`
names) are not requested by name. Their origin is whatever the transaction
resolves: the factory publishes `libva`, and `libva-intel-media-driver` may
pull it in, so read the resolved origin from the build's
`/usr/share/utah/package-origins.txt` rather than assuming Fedora's build.
Utah also requests `libvpl`, which is not an overrides name but a dependency
of `intel-vpl-gpu-rt`, so the media request is five packages in total.

`gstreamer1-plugins-bad-free` and `totem-pl-parser` are published package
names but are omitted from the install request because their dependency
closures are unsatisfied in the pinned factory inputs: bad-free needs
`libSoundTouch.so.2`, `libfaad.so.2`, `libopenal.so.1`, and `libsrtp2.so.1`,
and Totem needs `libuchardet.so.0`. Factory builds of `soundtouch`, `faad2`,
`openal-soft`, `libsrtp`, and `uchardet` are prerequisites, tracked by #383.
These are not absent package names or flaky repository failures; do not add
them to `[unavailable]` or count them as installed. The pin stays unchanged.
After closure publication, require `just check-repos` against the reviewed
pinned inputs before restoring either request.

Package installation does not prove codec functionality. On Intel hardware,
run `vainfo` against the render device and confirm the iHD driver loads and
advertises the expected decode profiles. Inspect `avdec_h264`, `openh264dec`,
and `vah264dec` with `gst-inspect-1.0`, then test a known H.264 sample.
The audit pin published `gstreamer1-plugin-openh264` but only `noopenh264`;
resolving that library dependency is not proof of a working decoder.

Remaining #383 gaps at the audit: `gstreamer1-plugin-libav`,
`gstreamer1-plugins-ugly-free`, `gstreamer1-plugin-dav1d`,
`papers-thumbnailer`, `gnome-epub-thumbnailer`, and `gst-thumbnailers`.
`ffmpegthumbnailer` is now consumed. Consume the rest only after factory builds
and dependency closures resolve against Utah's pinned inputs. `totem-pl-parser` is not a
replacement for those thumbnailers. Full FFmpeg versus `ffmpeg-free` remains
a maintainer policy decision; keep #383 open for hardware and codec proof.

## Repository policy

Runtime repositories are the pinned `utah-packages` repository (listed first)
plus Hummingbird's own repository only. **Fedora repositories are never
enabled at runtime** — they are bootstrap material for the package factory's
buildroot, not a source of installed packages (Containerfile package-RUN
comment, `Containerfile` ~L168; repo files copied at `Containerfile` L59).
`Containerfile.kernel`'s builder stage may use the pinned Fedora 44 repository
(`packages/fedora-44.repo`) strictly as a builder-only toolchain.

Every allowlisted repository is attested on two axes. Its **origin** is pinned
in `[repositories.baseurls]`: `verify-rpm-contract.py` fails a build that
enables an allowlisted repository with a different `baseurl`, a `metalink`/
`mirrorlist` (which DNF merges with any `baseurl` the section declares), or no
`baseurl` at all. Its **fetch integrity** is attested too: the same check
rejects `proxy=`, `sslverify=0`, `gpgcheck=0` (or its libdnf5 alias
`pkg_gpgcheck=0`), and `repo_gpgcheck=0` on an allowlisted repository (#345).
`proxy` and `sslverify=0` reroute or blind the fetch and are never approved;
`gpgcheck`/`repo_gpgcheck` disable RPM signature verification and are rejected
unless the repository is named in `[repositories.security]` with the option it
is approved to leave disabled (`gpgcheck` covers both `gpgcheck` and
`pkg_gpgcheck`). A repository not named there may not explicitly disable
signature verification (an omitted option falls back to the dnf5 default and
is not rejected). The same options set to a disabled value in the resolved dnf5
`[main]` configuration are always rejected, since they apply to every
repository and no per-repository approval covers them. A
`[repositories.security]` entry for a repository not in `[repositories.allowed]`
is rejected as approving nothing, as is any listed option other than
`gpgcheck` or `repo_gpgcheck`. The two documented exceptions are
`utah-packages` (RPMs are authenticated by the pinned package image and its OCI
provenance, not an RPM GPG key, so both signature checks are disabled) and
`nvidia-container-toolkit` (NVIDIA signs only its repomd.xml, so only package
signature verification is disabled).

A third axis, the **trust anchor**, is gated by `[repositories.gpgkeys]` (#617).
`gpgkey=` rewrites where a repository fetches signing keys from; an unpinned
entry lets a drop-in reroute the trust anchor to an attacker-controlled key
server. All sections declaring `gpgkey=` must match `[repositories.gpgkeys]`;
drop-ins cannot set `gpgkey=` on any section. Details and override rules live in
[`references/repository-authenticity.md`](references/repository-authenticity.md).

The install-source identity is single-sourced in `packages/*.repo`. Each repository
participating in the package install transaction carries a `# utah-install: true`
annotation (either directly preceding or within the `[section]` header in
`packages/utah-packages.repo` and `packages/hummingbird.repo`).
`scripts/install-packages.py` derives the `--enablerepo` set from these annotations
ordered by priority (ascending), so rebuilds in `utah-packages` (`priority=1`)
precede base Hummingbird packages (`priority=10`). Repositories without this marker
(such as `nvidia-container-toolkit` or builder-only `fedora-44`) are excluded from
the desktop package transaction.

The pinned package image is an RPM repository, not a runtime dependency. It is
bind-mounted into the package-contract and flavor-specific install RUN steps in
[`Containerfile`](../../Containerfile), both identified by
`--mount=type=bind,from=packages,source=/repository,target=/etc/utah-packages,ro`,
and never copied into a layer: a COPY of the whole ~4 GB repository would leave
a permanent layer behind, so reproducibility now comes from the digest-pinned
`packages` stage being the only source the package transaction can see rather
than from the repository contents living in the image.

The allowlist also runs **on-image**, against the composed image's runtime RPM
repositories, not just the source files in `packages/`. `verify-rpm-contract.py`
scans every `reposdir` dnf5 resolves at runtime, not a hardcoded list of
defaults (#454, #513, #536). It also scans dnf5's repository override
directories, `/etc/dnf/repos.override.d` and
`/usr/share/dnf5/repos.override.d` (#524).

- The `reposdir=` option in `/usr/share/dnf5/libdnf.conf.d/*.conf`,
  `/etc/dnf/libdnf5.conf.d/*.conf`, or `/etc/dnf/dnf.conf` replaces the
  documented default list. The gate loads these `[main]` configs in dnf5's
  order — drop-ins merged by file name (an `/etc` file masks a same-named
  `/usr/share` file) and applied sorted by file name, then `dnf.conf` — and
  uses the last-set value if any, so a custom reposdir the base image
  configures is scanned instead of the three defaults (#536).
- Without `reposdir=` configured, the gate falls back to dnf5's documented
  defaults — `/etc/yum.repos.d`, `/etc/distro.repos.d`,
  `/usr/share/dnf5/repos.d` — so a `.repo` file the base ships anywhere in
  those paths is subject to the same allowlist (#454, #513). A repo file the
  base ships in `/etc/distro.repos.d` or `/usr/share/dnf5/repos.d` is enabled
  at runtime exactly as one in `/etc/yum.repos.d`, so scanning only the
  first would leave it invisible to the gate (#513).
- A `proxy=` or `sslverify=0` in the resolved `[main]` section of the same dnf5
  configs applies to every allowlisted repository, so the gate resolves
  `[main]` the same way (later file wins, an empty `proxy=` clears an earlier
  one) and fails if the effective value sets a proxy or disables TLS
  verification (#352).
- The override drop-in dirs are scanned **unconditionally**, as a separate loop
  never folded into the `reposdir=`-derived list (#524). dnf5 reads them as
  fixed constants -- a base image setting `reposdir=` does not add or remove
  them (#536) -- so the gate scans them regardless of the runtime list; an
  image that points `reposdir=` elsewhere still gets override coverage instead
  of silently dropping it. A `.repo` override drop-in is partial by design: a
  `[id]` section may set only `enabled=`/`priority=` with no `baseurl=` (that is
  how the base disables a repo it ships), so the gate validates such a partial
  override only for the keys it sets -- allowlist membership, the
  `proxy=`/`sslverify=`/`gpgcheck=`/`pkg_gpgcheck=`/`repo_gpgcheck=` security
  options, and the absence of any `gpgkey=` (a drop-in that names a trust
  anchor cannot tell which keys the underlying repo shipped, so it is
  rejected outright, even on an allowlisted id) -- but never rejects it for a
  missing `baseurl=`. A partial override that leaves `enabled=` unset (for
  example `priority=` only) does not enable the repo, so it passes for any
  id unless it sets a `proxy=`, disables `sslverify=`, fails an unapproved
  signature check, or sets `gpgkey=`. A drop-in that sets any origin key
  (`baseurl=`, `metalink=` or `mirrorlist=`) is pinned like any other enabled
  repo.
- dnf5 matches override section names against repo ids as **globs**, so a
  `[*]` or `[utah-*]` section applies to every matching repo. The gate cannot
  enumerate those matches, so a wildcard override passes only when it cannot
  widen the allowlist: no origin key, no `enabled=1`, no `proxy=`, no disabled
  `sslverify=`, no signature check, no `gpgkey=` (#617). A `[*]` drop-in that
  sets only `priority=` or `enabled=0` passes.

## Printing and scanning gaps

CUPS and its driverless IPP support do not supply the full printing/scanning
stack (#390). `bluez-cups` belongs in `[parity]`: the factory publishes it as
a separate Bluetooth printer backend, and installing CUPS alone does not
request it. It requires the matching `bluez` build, so validate the full
transaction with `just check-repos` when changing this entry.

The factory pin inspected for #390's audit had no packages for the remaining
families: `system-config-printer`, `hplip`, `sane-backends`,
`sane-airscan`/`libsane-airscan`, `libsane-hpaio`, `ipp-usb`, `gutenprint`,
`foo2zjs`, `c2esp`, `dymo-cups-drivers`, `printer-driver-brlaser`,
`ptouch-driver`, `splix`, `braille-printer-app`, `paps`, and `mpage`.
Recheck the current Containerfile pin before adding them: they need factory
publication and dependency closure, not Fedora runtime repositories.
Prioritize the printer configuration tool, HP support and AirScan as
requested in #390, then resolve the published RPM names and their
dependencies against the pinned inputs. A metadata name match is only a
preflight: require transaction resolution, then verify printer
discovery/setup and scanning on hardware before claiming the cluster works.
Keep #390 open until the remaining work is covered.

## Supply-chain download verification

Every executable release asset fetched during image or ISO composition is
version-pinned and verified against a committed digest or published checksum
before extraction or execution. `scripts/check-download-integrity.py` runs
in `just check` and pre-commit to statically enforce that no build recipe
resolves a mutable latest release or downloads unverified executables.

## Install and verify cannot disagree

`scripts/install-packages.py` records exactly what its run resolved to
`/usr/share/utah/contract.txt`, and `scripts/verify-rpm-contract.py` asserts
*that file* in an image build rather than recomputing the set — the two
drifted once, so a contract package was installed and never verified
(install-packages.py:~125, verify-rpm-contract.py:~60). The manifest path in
the verifier is only the off-image `--check` fallback and asserts nothing
about installation.

The install transaction follows a strict execution sequence tested in
`tests/test_package_install.py`:
1. **Contract record**: The resolved contract packages (excluding `[build]`
   tooling and `[unavailable]` packages) are written to
   `/usr/share/utah/contract.txt`. If the path is unwritable, the script warns
   and continues (fails open).
2. **Install**: DNF runs with `--disablerepo=*`, enables only marked
   `# utah-install: true` repositories in ascending priority order, excludes
   `PackageKit*`, and installs the contract plus `[build]` tooling.
3. **Mark user**: `dnf mark user` marks all installed contract packages and
   build dependencies as user-installed before excluded package removal. This
   prevents DNF autoremove cascades from uninstalling contract packages (such as
   `xdg-desktop-portal-gnome`).
4. **Excluded removal**: `rpm -qa` is queried for packages declared in
   `[excluded]`. Only those actually present are removed with
   `dnf remove --no-autoremove`. Position after the subcommand is mandatory
   for DNF5 compatibility.

On NVIDIA flavors (`IMAGE_FLAVOR=nvidia` or `nvidia-gaming`),
`scripts/verify-rpm-contract.py` also asserts that the kernel module
(`extra/nvidia/nvidia.ko`) is present for every bootable kernel in the image and
that userspace tools (`nvidia-smi`, `nvidia-driver-version`) exist. Determining
the base kernel release cannot rely solely on `rpm -q kernel`, because `kernel`
is a metapackage that may not be installed on a minimal bootc base, and rpm queries
may return nothing or unhelpful text such as `package kernel is not installed`. If
no release resolves from rpm (empty or whitespace output), or if the resolved
release does not correspond to a directory under `/usr/lib/modules/<release>`, the
verifier falls back to the module trees present on disk under `/usr/lib/modules/`
(excluding the OGC gaming release for the base check). Furthermore, the verifier
guards against empty release strings, refusing to construct module paths from empty
releases or emit missing-module errors with empty kernel names.

## Failure semantics

- A contract package or dependency missing from the installation transaction
  is a **build failure**. `just check-repos` reads the base and package-image
  digests from `Containerfile`, copies the same repository configuration, and
  runs `install-packages.py --resolve` inside that base. This includes the
  release-specific Bluefin section, GNOME, services, and extension build tools.
  It needs Podman and network access. A name lookup on GitHub Pages is not
  evidence that the pinned OCI repository is complete or ABI-compatible.
  The factory's leading metadata layer is checked against the pinned manifest
  and blob hashes, then mounted for resolution without downloading RPMs. The
  factory must copy the same repodata into the leading layer and payload layer.
  DNF's `--assumeno` may return 1 for a valid declined transaction; the checker
  requires a transaction summary and rejects dependency and repository errors.
- `[unavailable]` entries still present in the install set are a validation
  error (`install-packages.py --check`).
- Drift in `packages/bluefin.toml` from upstream at `packages/.bluefin-parity-ref`
  is a CI failure (`just check-parity`).

## Pinned upstream parity reference

`packages/.bluefin-parity-ref` holds the 40-character commit SHA that
`packages/bluefin.toml` is synchronized with. The reference exists so Utah's
parity gate tests against a known revision rather than moving with Bluefin's
default branch, preventing unrelated upstream changes from breaking Utah's CI.

`.github/workflows/update-bluefin-parity.yml` moves it: nightly it resolves
Bluefin `main`, and when the upstream contract differs it opens or refreshes a
single review PR on `automation/bluefin-parity` carrying the new
`packages/bluefin.toml`, the new SHA here, and the regenerated counts. It never
auto-merges. Editing the reference by hand is only needed when synchronizing
`packages/bluefin.toml` outside that workflow.

The bump PR does not refresh `baselines/audit-baseline.json`: the audit is
local-only (it resolves names against the pinned factory repository), so the
workflow cannot run it. After merging a bump, run
`just audit-bluefin-parity --write` locally and commit the refreshed baseline;
until then `just check-audit-parity` reports a stale baseline because the
recorded `ref` no longer matches `packages/.bluefin-parity-ref`. The bump PR
body repeats this reminder.

**Do not commit overlay fixes to `automation/bluefin-parity`.** That branch is
disposable: `create-pull-request` rebuilds it from `main` plus the generated
working-tree changes on every run and force-resets it whenever the result
differs, so a `packages/utah.toml` fix pushed onto the open bump PR is
discarded at the next nightly run while upstream still differs from `main`.
Raise the overlay change as its own pull request against `main`; the bump PR
then picks the fix up on its next rebuild.

Current counts, per the README "Package parity" section: 61 Bluefin contract
packages installed, 112 Utah additions (GNOME 51, base-image parity, device
firmware, desktop services), 7 genuinely unavailable. `scripts/check-doc-counts.py` (part of
`just check`) recomputes these from the manifests and fails if either
document drifts from `site/data/packages.json`.

## A section nobody reads installs nothing

`contract()` in `scripts/install-packages.py` composes the install set from an
explicit list of overlay sections. A section added to `packages/utah.toml` but
not named there is read by nobody: the build succeeds, `just check` passes, and
the packages silently never install — the same failure this contract exists to
catch, one level up. Adding a section means four edits, not one: the section
and its header entry in `packages/utah.toml`; `contract()`; both buckets in
`scripts/verify-rpm-contract.py` (the `/usr/share/utah/contract.txt` path and
the off-image fallback) and its printed count; and `GROUPS` in
`scripts/generate-site-data.py`, which is what puts the card on the status
page. `tests/test_package_resolution.py` and `tests/test_verify_rpm_contract.py`
assert a package in every section reaches the install set and is counted under
its own heading, so a section wired into one place and not another fails the
suite rather than shipping quietly.

## Re-runnable parity audit (issue #402)

`scripts/image-baseline.py` measures what files Bluefin ships that Utah
does not (`baselines/GAP.md`). It does not say *where* the missing name
could come from, only that it is missing. That gap was the 2026-09-30
bare-metal audit (#382): `rpm -qa` both images, `comm` the difference,
then partition each gap name by which repository could supply it.

The `EXTRACT` script inside that tool globs a closed list of user-visible
paths (applications, autostarts, sessions, systemd units, `/usr/bin`,
`/usr/sbin`) and the Bluefin firefox-config defaults
(`/usr/share/ublue-os/firefox-config/*`, #502). The glob is `*`, not
`*.js`, because `99-flatpaks.sh` copies the whole directory: a narrower
pattern would let a non-`.js` file ship unseen. Adding a path means
adding a glob AND a `KINDS` entry so `write_report()` can classify the
new rows. `GapTests` in `tests/test_image_baseline.py` exercises missing
Firefox defaults in the report and recognizes an unowned overlay as shipped.
For extraction proof, run `extract IMAGE /tmp/surface-check` against a real
image and inspect the Firefox rows and asset contents; source-string checks
cannot establish shipping. Never append rows measured from a newer image to
an older snapshot: `image.txt` must describe the same image as both TSVs.

`scripts/audit-bluefin-parity.py` is the re-runnable version of that
pipeline. Every name Bluefin ships that Utah does not install (and does
not list in `[unavailable]`) lands in one of three partitions:

- **hummingbird-available** — resolves against Hummingbird today. The
  smallest move to close the gap is to add the name to `[parity]`
  (or `[hardware]`, etc.); the manifest is the only thing in the way.
- **factory-built** — absent from Hummingbird, present in the pinned
  factory repository. The factory already builds what we need; this is a
  manifest gap, not a factory gap.
- **nowhere** — neither repository provides it. A factory recipe must
  land first; until then, the name belongs in `[unavailable]` with a
  tracking issue.

The script reads the same pinned inputs as `just check-repos` (the
Containerfile `PACKAGE_IMAGE_SHA` for the factory OCI; the baseurl in
`packages/hummingbird.repo` for Hummingbird) and parses the
`primary.xml` from each, so the verdict and the install transaction
cannot disagree on what the repositories offer. No podman run is
involved — the audit is a static repodata read.

The audit writes `baselines/audit-baseline.json` (only when the
`--write` flag is passed; the default is report-only, matching the
2026-09-30 audit's "look before you leap" posture). The check
subcommand compares the current run to the baseline and exits nonzero when
a partition grows past the recorded state; a name moving from
`factory-built` to `hummingbird-available` is a Hummingbird rebuild
landing and is silent. A name disappearing from the baseline (an operator
moved it into `[parity]` and closed the gap) is silent too — only new
names that did not exist anywhere in the baseline trigger the gate.

The baseline records the Bluefin ref and factory pin it was captured
against (`ref` / `factory_ref` in the JSON). `check` compares those back
against the current audit before it diffs the partitions: a Bluefin-ref or
factory-pin bump that leaves the package set unchanged would otherwise read
as "no growth" and pass silently, so it is reported as a stale baseline
instead. Rewrite the baseline against the new ref with `--write` before the
gate can meaningfully run. A stale-baseline verdict is reported before any
partition-growth message, so it is never masked by a growth report, and the
failing summary line names the stale baseline rather than claiming the
partitions grew.
The baseline also records the Hummingbird repo `baseurl` it was captured
against. A Hummingbird repo URL change moves packages between the
Hummingbird and factory repodata the audit reads, shifting
`hummingbird-available` without any name actually being added or
removed, so the partition diff alone would read "no growth". The check
compares the current `baseurl` to the recorded one first and reports a
mismatch as a stale baseline before the partition diff, so the verdict
is never masked by a growth report; a baseline written before the key
existed is not invented into a mismatch. Rewrite the baseline against
the new repo with `just audit-bluefin-parity --write`.

Bootstrap is a one-time manual command: on a fresh checkout where
`baselines/audit-baseline.json` is missing, `just check-audit-parity`
exits 2 with a clear message; running `just audit-bluefin-parity --write`
once commits the starting state of the debt and turns the gate on.
Subsequent runs gate against that baseline.

```bash
just audit-bluefin-parity              # partition + print, do not write
just audit-bluefin-parity --write      # record the new baseline
just check-audit-parity                # fail on partition growth
just check-audit-parity --ref=HEAD     # audit against an unpinned Bluefin ref
```

The recipes read their flags from a `*args` parameter that is interpolated
into the shebang body with `{{args}}`. A `just` shebang recipe receives no
positional parameters (`$# = 0`), so a `"$@"` loop there is a silent no-op:
the flags never reach the script and the recipe falls back to report-only
`run`. Value flags use the `--key=value` form, which is what the recipe's
`case` re-parses.

The audit needs network (the factory OCI metadata layer and Hummingbird's
`repodata/`); it is a sibling of `just check-repos`, not part of `just
check`, which stays offline.

## Verification

```bash
just check-parity
just check-repos
just check-audit-parity
python3 scripts/install-packages.py --check packages/bluefin.toml
python3 scripts/verify-rpm-contract.py --check packages/bluefin.toml
python3 scripts/check-doc-counts.py
```

## Runtime ujust dependencies

See [the runtime ujust dependency reference](package-contract/references/ujust.md)
for provider checks and the retained audit baseline. Keep detailed inventories
in references so policy additions stay within the skill size budget.
