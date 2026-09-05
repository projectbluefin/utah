---
name: flavors
version: "1.0"
last_updated: "2026-09-05"
id: flavors
one_line_purpose: Add, remove, or retire an image flavor safely.
entry_point: docs/skills/flavors.md
category: image-build
mcp_compliance_level: partial
optimization_status: draft
status: active
dependencies: []
tags: [flavors, matrix, workflows]
description: >-
  config/flavors.json is the single source for build, promote, and release
  matrices. Use when changing the flavor set or debugging a matrix that
  names an image nothing builds.
metadata:
  type: procedure
---

# Flavors

Utah builds four `x86_64` flavors -- `main`, `nvidia`, `gaming`,
`nvidia-gaming` -- matching Bluefin's, on a single `testing` stream.
`config/flavors.json` decides which images exist, and everything derives from
it: the build matrix, the promote matrix, the release matrix, and whether the
kernel cache image is built at all.

```bash
python3 scripts/flavors.py list          # ["main", "nvidia", "gaming", "nvidia-gaming"]
python3 scripts/flavors.py needs-kernel  # true
```

## Retire a flavor through the `retired` map

A flavor is switched off by removing it from `flavors` and recording why
under `retired`, which keeps the reason beside the list rather than in a
commit message. Nothing else has to change: the scripts, the Containerfile
stages, and the cache image all stay put either way. `retired` is not
commentary -- it is what a flavor moves to when it is switched off
(docstring, `scripts/flavors.py`).

## No workflow literals

`just check` fails if any workflow names `utah-nvidia` or `utah-gaming`
directly (`Justfile`, `check` recipe; the guard is
`! grep -rn 'utah-nvidia\|utah-gaming' .github/workflows/`). No workflow may
carry its own copy of the flavor list -- that drift is what
`config/flavors.json` exists to stop. The literals were previously duplicated
across three workflows, so narrowing the build matrix while promote and
release still named images nothing produces failed late (recipe comment,
`Justfile`, `check`).

## The matrix is split in two, by what each flavor builds on

The build workflow resolves the set in its "Resolve the image flavors" step
and submits it as two matrices, `build_main` and `build_kernel` (comment,
`.github/workflows/build.yml`). `main` uses the pristine Hummingbird base;
the other flavors use the kernel cache image. The split exists so that a
kernel cache miss -- twenty minutes of compiling -- holds up only the flavors
that consume it, instead of every image in the run, and `main` starts the
moment the contract check passes. The two calls carry different `brand_name`
values because the reusable workflow keys its cancel-in-progress concurrency
group on that name; Utah's Justfile ignores it when naming images.

## flavors.py renders the set in the shape each consumer needs

`scripts/flavors.py` is the only reader of `config/flavors.json`:

- `list` -- every active flavor, for the build matrix.
- `list-main` -- flavors that build on the pristine base (never wait for the
  kernel cache).
- `list-kernel` -- flavors that build on the kernel cache image.
- `needs-kernel` -- `true`/`false`; with no OGC or NVIDIA flavor in the set,
  building the cache is 45 minutes spent on nothing.
- `images` / `releases` -- the same set shaped for the promote and release
  matrices (docstring, `scripts/flavors.py`).

Unknown names in `flavors` are a hard error at read time, so a typo in the
config fails before any matrix is built from it.

## Verification

```bash
python3 scripts/flavors.py list
just check
```
