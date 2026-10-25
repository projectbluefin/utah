---
name: docs-parity
version: "1.0"
last_updated: "2026-10-15"
id: docs-parity
one_line_purpose: Reconcile Utah's README with the docs.projectbluefin.io/utah page before deleting duplicate prose.
entry_point: docs/skills/docs-parity.md
category: meta
mcp_compliance_level: partial
optimization_status: draft
status: active
dependencies: [skill-improvement]
tags: [docs, readme, projectbluefin.io, renderer, parity, reconciliation]
description: >-
  README is the upstream source for docs.projectbluefin.io/utah; the docs page
  is rendered at build time. Use when the README changes touch what the docs
  page shows, when the docs page gains a live component, or when reconciling
  the two for a release.
metadata:
  type: procedure
---

# README ↔ docs.projectbluefin.io parity

Utah's user-facing docs page at
[`docs.projectbluefin.io/utah`](https://docs.projectbluefin.io/utah) is
the **target state** for a shared build-time renderer in
[`projectbluefin/documentation`](https://github.com/projectbluefin/documentation)'s
`scripts/fetch-readmes.mjs`. Today that renderer fetches only
`projectbluefin/server`'s README; `docs/utah.mdx` is still hand-written
prose. Once the Utah source is added there, `README.md` becomes the
canonical source. Until then, edit the README **and** the docs wrapper in
the same change, or the two will drift.

This skill is the reconciliation contract: what the README owns, what the
docs page owns, and how to land changes without either one of them drifting
into the other.

## What the README owns

Everything that is **true of the source repository right now**:

- Identity, architecture, and the two-repository composition with
  `projectbluefin/utah-packages`.
- The honest pre-alpha status, including which artifacts are published: the
  testing live ISO, the `:testing` consumer tag after `post-testing-e2e`
  validates a digest, and the rolling `:stable` tag that `execute-release.yml`
  moves on each testing-to-main promotion behind the release gate (kept as the
  interface by the #336 ruling).
- The concise Known gaps list (Secure Boot and live-media boot paths, bootc
  update timers, ESP maintenance via `bootctl`, hardware/codec/driver
  validation, CUDA exclusion) — one bullet per gap; the docs page never
  carries a long form.
- The Package parity table — both numbers and the byte-for-byte contract.
- Verification record and provenance links (CI runs, screenshots).
- The "Download and image streams" section: the testing ISO link and its
  SHA-256 checksum link, the pointer to `docs.projectbluefin.io/utah` for live
  versions and downloads (the catalog lives there, not in the README), the
  canonical flavor set from `config/flavors.json`, and the stream table.

## What the docs page owns

What the README **cannot** carry honestly:

- The live `DriverVersionsCatalog` (kernel, Mesa, NVIDIA, GNOME — version,
  provenance, update date, version history, image switch commands, reboot
  guidance). It is generated data backed by
  [`static/data/driver-versions.json`](https://github.com/projectbluefin/documentation/blob/main/static/data/driver-versions.json)
  and rendered by
  [`DriverVersionsCatalog.tsx`](https://github.com/projectbluefin/documentation/blob/main/src/components/DriverVersionsCatalog.tsx).
  Its `utah-testing` snapshot is empty today — the catalog displays "No
  driver version data is published for this stream yet" until the build
  pipeline starts populating it. Freeze that empty state in prose and the
  page lies the moment data lands.
- The Edit-this-page link, which points at `README.md` on `projectbluefin/utah`
  `main` (set by the docs wrapper, not the README).
- Sidebar and routing metadata (`slug: /utah`, position under Images after
  `dakota`, `server`, `classic`).

## Reconciliation record

Tracked by issue #526. The README is reconciled against the previous docs
page as follows:

| Topic | Previous docs page claim | Reconciled state | Lives in |
|---|---|---|---|
| Identity | "Utah is Bluefin built on Fedora Hummingbird … Utah adds the desktop layer" | Same identity, plus the Fedora Hummingbird Magazine link | README "What it is" |
| Two-repo composition | "Utah is composed across two coordinated repositories" with `projectbluefin/utah-packages` | Same two-repo table | README "What it is" |
| Pre-alpha status | "Utah is an alpha build … its `:testing` container image is published on GHCR" | Testing container images and live ISOs are available for hardware testing; the `:testing` consumer tag is published on `ghcr.io/projectbluefin/utah:testing` once `post-testing-e2e` validates a digest; the rolling `:stable` tag moves on each testing-to-main promotion behind the release gate | README header + "Download and image streams" |
| Download | `https://projectbluefin.dev/utah-live-latest.iso` | Same testing ISO URL plus its `-CHECKSUM` link, and a pointer to the docs page for live versions and downloads | README "Download and image streams" |
| Image streams | `utah`, `utah-nvidia`, `utah-gaming`, `utah-nvidia-gaming` on stable/testing | Same set; canonical source is `config/flavors.json` | README "Download and image streams" |
| Features / stack | Kernel options + OGC + Gaming mode; NVIDIA and Gaming flavors; GNOME 51 from source | Same explanations, with the precise kernel/NVIDIA/Secure Boot caveats | README "Known gaps" |
| Current Versions | `<DriverVersionsCatalog streamId="utah-testing" />` (live, empty today) | Component kept; README does not duplicate it | docs page only |
| Further reading | Repo + utah-packages + Fedora Hummingbird Magazine | Same three links | README "What it is" + docs page |
| Verification, screenshots, CI runs | n/a (docs page) | Screenshots, CI run link, package counts | README (top + "Package parity with Bluefin") |
| Detailed gaps | n/a (docs page) | Concise one-bullet-per-gap list | README "Known gaps" |

Items duplicated by the previous docs page that the docs wrapper drops after
this PR lands:

- The hand-maintained stream table (`ghcr.io/projectbluefin/utah:stable` /
  `:testing`). The README already names the streams and the canonical flavor
  set; pulling them again into the docs wrapper duplicates a volatile inventory
  that the catalog will eventually drive.
- The hand-maintained GNOME-version statement. The `DriverVersionsCatalog`
  is the live source.
- Any hand-written download URL in the wrapper. The README carries the
  testing ISO and checksum links; duplicating them in the wrapper invites
  drift.

## Editing protocol

When changing the README in a way the docs page surfaces:

1. Make the change in `README.md` on a feature branch against
   `upstream/main`.
2. Run `just check`. The package-parity count check
   ([`scripts/check-doc-counts.py`](../../scripts/check-doc-counts.py))
   trips first if the prose drifts from the manifests.
3. Open the PR against `projectbluefin/utah`'s `main` branch (the docs
   renderer fetches `main`). PR title is a Conventional Commit (`docs:`,
   `fix:`, `feat:`); the merge rebuilds the docs site.
4. After the merge, `docs.projectbluefin.io/utah` is updated by the
   documentation repo's next `pages.yml` push (builds on push, not on a
   schedule), which fetches the new README, resolves relative links, and
   re-renders. No browser-side fetch; no silent stale fallback (the
   renderer's contract — see `docs/skills/variant-docs-pages.md` in
   `projectbluefin/documentation`).

When adding or changing a live component on the docs page (a catalog, a
switch-command widget, a download-button):

1. Land the change in `projectbluefin/documentation` first; the renderer
   must be able to fetch the new README without breaking the existing page.
2. Reference the new component from the README's "Download and image
   streams" section (which links `docs.projectbluefin.io/utah`) so users can
   find it from the upstream source.
3. If the new component replaces prose the README is currently carrying,
   delete the prose **after** the docs-side change has rendered and the
   empty state has been visually confirmed.

## What this skill does not cover

- The doc wrapper file (`docs/utah.mdx`) itself — that lives in
  `projectbluefin/documentation`. Utah only owns the README.
- The catalog data source — Utah does not publish driver versions today; when
  it does, that pipeline lives wherever the build artifacts land.
- Markdown renderer bugs in `projectbluefin/documentation` — file them in
  that repo, not here.
