# Utah Skill Router

Agent entry point for `projectbluefin/utah`. Find the source that matches your
task, load only that, then act. Utah's deep documentation lives in
[`skills/`](skills/) — the skills in the task index below. The Containerfile's
comment blocks and the manifests' headers stay canonical for mechanism detail
next to the code, and the skills cite them.

## Read order

1. [`AGENTS.md`](../AGENTS.md) — repo contract, build commands, invariants.
2. This file — task → source mapping.
3. The source named in the table below.
4. `projectbluefin/common` `docs/skills/factory-onboarding.md` for cross-repo
   factory rules.

## Task index

| I need to... | Read |
|---|---|
| Build the image locally (quickstart) | [`building.md`](building.md#build-locally) |
| Add, remove, or debug packages, or fix parity/check-repos failures | [`skills/package-contract.md`](skills/package-contract.md) |
| Add, remove, or retire an image flavor | [`skills/flavors.md`](skills/flavors.md) |
| Work on the OGC kernel or NVIDIA module cache | [`skills/kernel-cache.md`](skills/kernel-cache.md) |
| Edit the `Containerfile`, add a script, or investigate build time | [`skills/containerfile.md`](skills/containerfile.md) |
| Validate changes end-to-end in a VM or live ISO | [`skills/local-testing.md`](skills/local-testing.md) |
| Change branding, desktop defaults, or first-boot Flatpak policy | [`skills/desktop-contract.md`](skills/desktop-contract.md) |
| Change `.github/workflows/` or debug a red run | [`skills/ci-workflows.md`](skills/ci-workflows.md) |
| Decide whether to write or update a skill | [`skills/skill-improvement.md`](skills/skill-improvement.md) |
| Onboard into the factory / cross-repo rules | common's `docs/skills/factory-onboarding.md` |

## Skill catalog

[`skills/index.json`](skills/index.json) and [`skills/index.md`](skills/index.md)
are the generated catalog of all skills, produced by
`python3 scripts/generate_skill_index.py --write`. They are machine output —
never hand-edit them; run the generator after changing any skill's front
matter.

## How to load

Read the named source in full before editing the area it covers. If a session
surfaces a non-obvious pattern or workaround, update the matching source (or
add a skill under [`skills/`](skills/)) in the same PR — see
[`skills/skill-improvement.md`](skills/skill-improvement.md).
