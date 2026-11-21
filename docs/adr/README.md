# Architecture decision records

Decisions that shape Utah beyond a single change: what was decided, why, what
was rejected, and what would reverse it. Skills under `docs/skills/` say how
things work today; an ADR says why they are that way, or why they are about to
change.

## Conventions

- One file per decision: `NNNN-short-title.md`, numbered in order, never
  reused.
- Status is one of **Proposed**, **Accepted**, **Rejected**, **Superseded by
  ADR-NNNN**. An accepted ADR is not edited to change the decision; a new ADR
  supersedes it, and the old one's status line points at the new one.
- Sections: Context (with evidence: run links, issues, file and line
  references), Decision, Consequences, Alternatives considered, and exit
  criteria when the decision is an experiment.
- Link the tracking issue, and add the ADR to the table below in the same PR.

## Index

| ADR | Title | Status |
|---|---|---|
| [0001](0001-image-based-installer.md) | Image-based installer (GNOME OS-style), in parallel with fisherman | Proposed |
