---
name: skill-improvement
id: skill-improvement
one_line_purpose: Capture durable agent learnings in maintained docs.
entry_point: docs/skills/skill-improvement.md
category: meta
status: active
tags: [skills, improvement, documentation]
description: >-
  The skill-improvement mandate for Utah — every agent session must produce a
  documentation update alongside the work when it learned something durable.
---

# Skill Improvement Mandate

Every agent session produces two outputs:

1. **The work** — the PR, fix, or improvement.
2. **The learning** — what a future agent needs to know.

Output 1 without Output 2 leaves the factory no smarter.

## Before you mark work complete

- [ ] Did I discover a workaround, non-obvious pattern, or convention?
- [ ] Is there an authoritative source for the area I worked in
      (see [`../SKILL.md`](../SKILL.md))?
- [ ] If yes — did I update it?
- [ ] If no — did I create a skill file here?
- [ ] Is the update committed in **this same PR**? Not a follow-up. Same PR.

## Where learnings go in Utah

Utah keeps deep documentation next to the code: `docs/building.md`, the
Containerfile comment blocks, manifest headers, and script headers. Update the
nearest of those first. Add a file under `docs/skills/` only when no existing
source owns the topic, and add it to the task index in
[`../SKILL.md`](../SKILL.md).

Factory-wide learnings go to `projectbluefin/common` as an issue with the
learning, affected component, and evidence.

## Banned

- Changelog files (`IMPROVEMENTS.md`, `CHANGELOG.md`, `SESSION.md`) — delete
  on sight.
- Session notes committed to the repo (`NOTES.md`, `PLAN.md`, `TODO.md`).
- "Append here" instructions — route to a maintained source instead.
