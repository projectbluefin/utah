---
name: diagnosis
version: "1.0"
last_updated: "2026-10-28"
id: diagnosis
one_line_purpose: Route running-machine diagnosis to the shipped Utah guidance.
entry_point: docs/skills/diagnosis.md
category: contracts
status: active
dependencies: []
tags: [diagnosis, on-device, agent]
description: Diagnose a running Utah machine using its shipped context and installed tool inventory.
on_device: docs/agent/skills/diagnosis.md
metadata:
  type: procedure
---

# On-device agent context

Read [the device diagnosis skill](../agent/skills/diagnosis.md) for runtime
commands and [the entry point](../agent/AGENTS.md) for boundaries.
The bundle installs at `/usr/share/utah/agent/` via the scratch input stage
and existing overlay RUN, without installing runtime code or new recipes.

Canonical skills opt into a reviewed device edition with `on_device` front
matter. Run `python3 scripts/generate_agent_context.py --write` after changing
the selection; `--check` in `just check` rejects missing, extra or stale
catalog entries and device editions. Keep checkout commands explicitly marked
as checkout work. Do not ship the repository's AGENTS.md as runtime guidance.
Common's shared context entry point is tracked in projectbluefin/common#1565;
this self-contained bundle does not depend on that pending convention.

Device documentation is an image build input: keep `docs/agent` in the
source-epoch path lists in the Justfile and reproducibility checker.
The existing desktop verifier requires bundle files during image composition;
add new device skill paths to `contracts/bluefin-desktop.toml` too.
