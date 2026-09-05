# Utah Skill Router

Agent entry point for `projectbluefin/utah`. Find the source that matches your
task, load only that, then act. Utah keeps its deep documentation next to the
code it describes — in `docs/building.md`, in the Containerfile's comment
blocks, and in the manifests' headers — rather than duplicating it here.

## Read order

1. [`AGENTS.md`](../AGENTS.md) — repo contract, build commands, invariants.
2. This file — task → source mapping.
3. The source named in the table below.
4. `projectbluefin/common` `docs/skills/factory-onboarding.md` for cross-repo
   factory rules.

## Task index

| I need to... | Read |
|---|---|
| Understand what the image is made of and how to build it | [`building.md`](building.md) |
| Change the package set or check Bluefin parity | `packages/utah.toml` header + [`AGENTS.md`](../AGENTS.md) invariants |
| Understand why `[multimedia_overrides]` is not installed | `packages/utah.toml` header comment |
| Edit the `Containerfile` | The "Layer discipline" comment block at the top of `Containerfile` |
| Add, remove, or retire an image flavor | [`skills/flavors.md`](skills/flavors.md) + `config/flavors.json` |
| Work on the OGC kernel or NVIDIA module cache | [`skills/kernel-cache.md`](skills/kernel-cache.md) + `scripts/kernel-cache-tag.sh` header |
| Build and boot a local VM or live ISO | [`building.md`](building.md#build-locally) |
| Change branding, desktop defaults, or first-boot Flatpak policy | `contracts/bluefin-desktop.toml` header + `scripts/verify-desktop-contract.py` |
| Change `.github/workflows/` | Comments in `.github/workflows/build.yml` + common's `docs/skills/ci-tooling/SKILL.md` |
| Understand where build time goes before optimizing | [`skills/containerfile.md`](skills/containerfile.md) |
| Decide whether / how to update a skill | [`skills/skill-improvement.md`](skills/skill-improvement.md) |
| Onboard into the factory / cross-repo rules | common's `docs/skills/factory-onboarding.md` |

## How to load

Read the named source in full before editing the area it covers. If a session
surfaces a non-obvious pattern or workaround, update the matching source (or
add a skill under [`skills/`](skills/)) in the same PR — see
[`skills/skill-improvement.md`](skills/skill-improvement.md).
