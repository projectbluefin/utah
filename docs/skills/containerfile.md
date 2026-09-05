---
name: containerfile
version: "1.0"
last_updated: "2026-09-05"
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

## Clean and lint share a layer

Everything above writes build-time residue that bootc lint rejects: dnf logs
under `/var/log`, cockpit and dnf state under `/run`, and ~45 `/var`
directories with no tmpfiles.d entry. `utah-clean-stage` must run after the
last package install, which is the NVIDIA and OGC step, not after the main
transaction. The lint that checks the result runs in the same layer
(`bootc container lint --fatal-warnings --skip nonempty-boot`): nothing can
change between the two (comment, `Containerfile`).

## Verification

```bash
just check
grep -c '^COPY\|^RUN' Containerfile
```

The count stays at its current value (13 as of 2026-09-05) unless the change
justifies a new layer against the timings table above.
