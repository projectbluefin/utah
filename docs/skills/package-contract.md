---
name: package-contract
version: "2.0"
last_updated: "2026-11-14"
id: package-contract
one_line_purpose: Maintain Utah's base manifest, overlay and Dakota parity triage.
entry_point: docs/skills/package-contract.md
category: contracts
mcp_compliance_level: partial
optimization_status: draft
status: active
dependencies: []
tags: [packages, parity, dakota, contracts]
description: >-
  Package contract: Utah-owned base.toml, utah.toml overlay, device
  firmware, [unavailable] rules, repository policy, Dakota gap triage
  (baselines/GAP.md). Use when adding, removing, or debugging packages or
  parity/check-repos failures.
on_device: docs/agent/skills/package-contract.md
metadata:
  type: policy
---

# Package Contract

Utah ships the Bluefin experience on a Hummingbird base. Its parity target is
Dakota (`ghcr.io/projectbluefin/dakota:testing`, Bluefin on GNOME OS). Two
manifests under `packages/` define what the image installs; this skill is the
policy for changing them.

## The two manifests

- **`packages/base.toml`** — Utah's base manifest. It was frozen from
  projectbluefin/bluefin's `build_files/packages/base.toml` at
  `bb63fd1667eb84209e0af2cc3772ca96e8f6bda9` (2026-09-17) and is edited by
  hand; nothing syncs it from Bluefin classic. Keep its section layout
  (`[multimedia_overrides]`, `[fedora]`, `[fedora_vNN]`, `[excluded]`):
  `scripts/install-packages.py` reads exactly those. Adds and removes follow
  from the Dakota gap report (see "Dakota parity" below).
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
  - `[unavailable]` — parity gaps none of Utah's enabled repositories
    provide, whether the name comes from `base.toml` or from a measured image
    baseline.

Flatpak-first (standing policy): no RPM for an end-user app Utah ships as a
Flatpak (`contracts/bluefin-desktop.toml` `[flatpak]`). Transitional native
payloads need an inline drop condition in `packages/utah.toml`; the factory
follows the same rule when importing recipes.

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
It covers both kinds of parity gap: names in `base.toml`, and names a measured
image ships outside that manifest (Dakota's gaps are triaged in
`baselines/triage.toml`; `nss-mdns` came from the retired Bluefin image
baseline). Each entry
**MUST carry a tracking issue**: the list is the documented parity debt, not
a dumping ground for packages that are merely inconvenient (header comment,
`packages/utah.toml`).

## Media packages and codec parity

The base manifest's `[multimedia_overrides]` (from Bluefin) selects replacement builds from negativo17;
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

The four requested overrides and `ffmpegthumbnailer` are in `[factory].parity`;
their installed releases must carry `.bfin` to prevent base substitution.
Check new names with `just check-repos` and the image's `package-origins.txt`.

The pinned factory (`ARG PACKAGE_IMAGE_SHA` in `Containerfile`) publishes `openh264` 2.7.0 and
`gstreamer1-plugin-openh264` 1.28.6, plus `totem-pl-parser` and `uchardet`.
Utah requests the plugin, real codec, and playlist parser by name and asserts
their factory release identity. The plugin's library requirement alone is insufficient: older
factory pins offered the `noopenh264` compatibility library instead of a
working decoder. Recheck the digest-pinned repodata before carrying forward
an older publication blocker.

Utah also consumes libav, dav1d, bad-free and ugly-free GStreamer plugins,
`ffmpegthumbnailer`, `gnome-epub-thumbnailer`, and the Papers thumbnailer.
Package installation does not prove codec functionality. On Intel hardware,
run `vainfo` against the render device and confirm the iHD driver loads and
advertises the expected decode profiles. Inspect `avdec_h264`, `openh264dec`,
and `vah264dec` with `gst-inspect-1.0`, then test a known H.264 sample.
The PR #919 decoder-load probe failed for H.264: the factory's `gstreamer1-plugin-libav` is built against `ffmpeg-free`, so `avdec_h264` never registers (#1116); `openh264dec` is the supported software H.264 path.
`utah-verify-desktop-contract` loads each `[media].decoders` entry in `contracts/bluefin-desktop.toml` with `gst-inspect-1.0` and blocks the image build on failure; `avdec_h264` returns only when the factory ships full FFmpeg.

Remaining #383 gaps include full FFmpeg (the decided direction, replacing
`-free`) and `gst-thumbnailers` (factory #527); neither is published in this
pin. Keep #383 open for those factory deliveries and hardware/codec proof.

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
rejects `proxy=`, `sslverify=0`, any `sslcacert=`, `gpgcheck=0` (or its libdnf5 alias
`pkg_gpgcheck=0`), and `repo_gpgcheck=0` on an allowlisted repository (#345).
`proxy` and `sslverify=0` reroute or blind the fetch and are never approved, and any
`sslcacert=` re-anchors TLS trust to a custom CA bundle and is likewise never
approved (#497);
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
- A `proxy=`, `sslverify=0`, or `sslcacert=` in the resolved `[main]` section of the same dnf5
  configs applies to every allowlisted repository, so the gate resolves
  `[main]` the same way (later file wins, an empty value clears an earlier
  one) and fails if the effective value sets a proxy, disables TLS
  verification, or names a CA bundle (#352, #497).
- The override drop-in dirs are scanned **unconditionally**, as a separate loop
  never folded into the `reposdir=`-derived list (#524). dnf5 reads them as
  fixed constants -- a base image setting `reposdir=` does not add or remove
  them (#536) -- so the gate scans them regardless of the runtime list; an
  image that points `reposdir=` elsewhere still gets override coverage instead
  of silently dropping it. A `.repo` override drop-in is partial by design: a
  `[id]` section may set only `enabled=`/`priority=` with no `baseurl=` (that is
  how the base disables a repo it ships), so the gate validates such a partial
  override only for the keys it sets -- allowlist membership, the
  `proxy=`/`sslverify=`/`sslcacert=`/`gpgcheck=`/`pkg_gpgcheck=`/`repo_gpgcheck=` security
  options, and the absence of any `gpgkey=` (a drop-in that names a trust
  anchor cannot tell which keys the underlying repo shipped, so it is
  rejected outright, even on an allowlisted id) -- but never rejects it for a
  missing `baseurl=`. A partial override that leaves `enabled=` unset (for
  example `priority=` only) does not enable the repo, so it passes for any
  id unless it sets a `proxy=`, disables `sslverify=`, sets `sslcacert=`, fails
  an unapproved signature check, or sets `gpgkey=`. A drop-in that sets any origin key
  (`baseurl=`, `metalink=` or `mirrorlist=`) is pinned like any other enabled
  repo.
- dnf5 matches override section names against repo ids as **globs**, so a
  `[*]` or `[utah-*]` section applies to every matching repo. The gate cannot
  enumerate those matches, so a wildcard override passes only when it cannot
  widen the allowlist: no origin key, no `enabled=1`, no `proxy=`, no disabled
  `sslverify=`, no `sslcacert=`, no signature check, no `gpgkey=` (#617). A `[*]` drop-in that
  sets only `priority=` or `enabled=0` passes.

## Printing and scanning gaps

CUPS and its driverless IPP support do not supply the full printing/scanning
stack (#390). `bluez-cups` belongs in `[parity]`: the factory publishes it as
a separate Bluetooth printer backend, and installing CUPS alone does not
request it. It requires the matching `bluez` build, so validate the full
transaction with `just check-repos` when changing this entry.

`[parity]` requests the scanner and driver families the factory publishes
(`sane-backends*`, `sane-airscan`, `ipp-usb`, `gutenprint*`, `hplip*`,
`libsane-hpaio`, `system-config-printer-{libs,udev}`, `foo2zjs`, `c2esp`,
`dymo-cups-drivers`, `printer-driver-brlaser`, `ptouch-driver`, `splix`,
`mpage`, `libieee1284`). `braille-printer-app` and `paps` are unpublished.
Bluefin ships no `system-config-printer` UI (GNOME Settings is it). The
`[parity]` "Left out" comment in `packages/utah.toml` lists what is still
blocked. Method:
[`references/factory-consumption.md`](package-contract/references/factory-consumption.md).
A metadata name match is only a preflight: require transaction resolution, then
verify printer discovery/setup and scanning on hardware before claiming the
cluster works. Keep #390 open until the remaining work is covered.

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
  release-specific base-manifest section, GNOME, services, and extension build tools.
  It needs Podman and network access. A name lookup on GitHub Pages is not
  evidence that the pinned OCI repository is complete or ABI-compatible.
  The factory's leading metadata layer is checked against the pinned manifest
  and blob hashes, then mounted for resolution without downloading RPMs. The
  factory must copy the same repodata into the leading layer and payload layer.
  DNF's `--assumeno` may return 1 for a valid declined transaction; the checker
  requires a transaction summary and rejects dependency and repository errors.
- `[unavailable]` entries still present in the install set are a validation
  error (`install-packages.py --check`).

## Dakota parity

`scripts/image-baseline.py` measures what Dakota ships that Utah does not:
`extract` lists the user-visible files (applications, autostarts, sessions,
systemd units, `/usr/bin`, `/usr/sbin`) inside each image, and `gap` writes
`baselines/GAP.md` with every Dakota path Utah lacks. Dakota has no RPM
database, so each path is attributed to the Dakota element most likely to
install it, guessed from `baselines/dakota/elements.tsv` (the SBOM of the
latest `testing` publish run) plus a short `ALIASES` table; an unmatched path
is its own item. Details and the refresh flow: `baselines/README.md`.

Every item needs an entry in `baselines/triage.toml` (`just check` runs
`image-baseline.py check`): `planned` with an issue (#1236 is the epic; its
children #1222-#1235 track the structural moves such as kanata, native
Ghostty, systemd-boot, AppArmor, oo7, sudo-rs, virtualization and service
policy), `waived` with a reason (GNOME OS or BuildStream internals,
Flatpak-first apps), `unavailable` for names in `[unavailable]`, or
`untriaged` with an `area`. Closing a gap means shipping the files Utah lacks,
usually by adding the Fedora package to `base.toml` or `utah.toml`, then
re-measuring and dropping the entry `check` reports as no longer a gap.

Never append rows from a newer image to an older snapshot: `image.txt` must
describe the same image as its `surface.tsv`; after a refresh,
[remove resolved triage entries](package-contract/references/factory-consumption.md#closing-baseline-gaps).

Current counts, per the README "Parity with Dakota" section: 61 base manifest
packages installed, 260 Utah additions (GNOME 51, base-image parity, device
firmware, desktop services), 6 genuinely unavailable. `scripts/check-doc-counts.py` (part of
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

## Verification

```bash
just check-repos
python3 scripts/install-packages.py --check packages/base.toml
python3 scripts/verify-rpm-contract.py --check packages/base.toml
python3 scripts/image-baseline.py check
python3 scripts/check-doc-counts.py
```

## Runtime ujust dependencies

See [the runtime ujust dependency reference](package-contract/references/ujust.md)
for provider checks. Keep detailed inventories
in references so policy additions stay within the skill size budget.
