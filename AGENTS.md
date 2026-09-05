# Utah — Agent Operating Contract

Utah composes [Bluefin](https://projectbluefin.io) on the Fedora Hummingbird
`bootc-os` base. Experimental pre-alpha: it has a build, not users. Nothing is
published — no image, no ISO artifact, no installer.

## Read order

1. This file — repo rules, build commands, and boundaries.
2. [`docs/SKILL.md`](docs/SKILL.md) — task → source router.
   `docs/skills/index.json` / `docs/skills/index.md` are the generated catalog
   of the skills it routes to.
3. [`projectbluefin/common`](https://github.com/projectbluefin/common)
   `AGENTS.md` and `docs/skills/factory-onboarding.md` — the shared-contract
   sidecar. Factory-wide rules; never overrides this repository's local
   authority.

## Build, test, and lint

```bash
just check                            # full static validation (run before every commit)
just check-parity                     # bluefin.toml must equal upstream base.toml (network)
just check-repos                      # contract packages resolvable in enabled repos (network)
just build-ghcr utah testing main     # local image build -> localhost/utah:testing
just check-desktop-contract           # in-image verifiers against a built image
just generate-bootable-image testing  # bootc install to-disk -> output/bootable.raw
just boot-vm                          # QEMU/noVNC; confirm GDM + GNOME Shell render
just iso testing && just boot-iso     # live ISO build + boot (see docs/skills/local-testing.md)
```

`just check` is the gate CI runs first; a change that fails it fails the whole
matrix. It needs the GNOME extension submodules initialized
(`git submodule update --init --recursive`) or the extension contract check
fails with missing `metadata.json` errors. It also runs the skill catalog
checks (`scripts/check-skill-frontmatter.sh`, `scripts/check-skill-index.sh`,
`python3 scripts/generate_skill_index.py --check`). Run `just check` and
`pre-commit run --all-files` before every commit.

## Invariants — do not break

- **`packages/bluefin.toml` is a byte-for-byte copy** of
  `projectbluefin/bluefin` `build_files/packages/base.toml`. Never hand-edit
  it; sync it verbatim from upstream. CI diffs it on every run
  (`just check-parity`).
- **Utah's own package changes live in `packages/utah.toml`** (`[gnome]`,
  `[build]`, `[unavailable]`). Every `[unavailable]` entry MUST carry a
  tracking issue. A missing contract package is a build failure, never a
  silent skip.
- **`config/flavors.json` is the single source of the flavor set.** No
  workflow may name `utah-nvidia` or `utah-gaming` literally — `just check`
  fails on it. Retire a flavor by moving it under `retired` with the reason.
- **Containerfile ARG digests are Renovate-managed pins** (`BASE_IMAGE`,
  `PACKAGE_IMAGE_SHA`, `COMMON_IMAGE_SHA`, `BREW_IMAGE_SHA`). Do not bump them
  by hand unless the task is exactly that. `Containerfile` and
  `Containerfile.kernel` must share the same `BASE_IMAGE` line; `just check`
  asserts it.
- **Layer discipline.** Read the comment block at the top of `Containerfile`
  before editing it: sources arrive in as few COPYs as origins allow, small
  RUN steps fold into neighbours, and per-image ARGs are declared late because
  an ARG is part of the cache key of every layer below it.
- **The kernel cache tag** is a hash of `Containerfile.kernel`'s base image,
  `scripts/install-ogc-kernel.sh`, `scripts/install-nvidia.sh`, and
  `packages/hummingbird.repo` (`scripts/kernel-cache-tag.sh`). Any edit to
  those files — comments included — forces a ~45-minute cache rebuild. That is
  deliberate; just know it before you touch them.
- **Utah scripts install as `/usr/local/libexec/utah-*`** via one staged COPY
  and a rename loop in the Containerfile. A new script means updating the COPY
  list, the rename loop, and `just check`.
- **`ENABLE_SSHD=1` is local-diagnostic only.** Never in a published image.
- **Fedora repositories are never enabled at runtime.** Packages come from the
  pinned `utah-packages` OCI repository and Hummingbird's own repository.

## What agents must not touch

- Any `ublue-os/*` repository — read-only, no writes of any kind.
- GNOME Shell extension submodules under
  `system_files/shared/usr/share/gnome-shell/extensions/` — vendored, managed
  through `.gitmodules` pins.

## PR rules

- Conventional Commits title (`feat:`, `fix:`, `docs:`, `ci:`, `chore:`).
- One logical change per PR; run `just check` before every commit.
- AI-authored commits include both attribution trailers:
  ```
  Assisted-by: <Model> via GitHub Copilot
  Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>
  ```
- Ask before opening PRs autonomously; prepare the branch and diff first.

## Self-Improvement

Every session: ship the work AND update the relevant skill file in
`docs/skills/`. Same PR. Not a follow-up.

`docs/SKILL.md` (the router) and `docs/skills/index.json` (the generated
catalog) are canonical. When skills change — added, removed, or front-matter
edited — regenerate the catalog with
`python3 scripts/generate_skill_index.py --write`; `just check` and pre-commit
fail on a stale catalog.

Banned:
- No changelog files. Delete `IMPROVEMENTS.md`, `CHANGELOG.md`, `SESSION.md`
  if found.
- No session notes committed to the repo (`NOTES.md`, `PLAN.md`, `TODO.md`).
- No "append here" docs. Route to `docs/skills/` instead.

Before marking work done:
- [ ] Discovered a workaround, pattern, or convention?
- [ ] Skill file updated (or created)?
- [ ] Committed in this same PR?

## See also

- [`README.md`](README.md) — user-facing overview and honest gap list.
- [`docs/building.md`](docs/building.md) — the contributor build quickstart.
- [`docs/skills/`](docs/skills/) — the canonical deep documentation, plus the
  generated catalog (`index.json` / `index.md`).
