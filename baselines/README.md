# Image baselines

What Dakota, Utah's parity target, actually ships, measured from the
published image, so Utah's parity is checked against reality rather than
against a manifest.

Dakota (`projectbluefin/dakota`) is GNOME OS built with BuildStream: it has no
RPM database, so package names cannot be compared. The comparison is by
user-visible file instead, and each Dakota file is attributed to the
BuildStream element most likely to install it.

| path | source |
| --- | --- |
| `dakota/surface.tsv` | every user-visible file inside `ghcr.io/projectbluefin/dakota:testing`, with the guessed Dakota element (or `(dakota)` when no element name matched) |
| `dakota/elements.tsv` | the SPDX SBOM Dakota's `testing` publish workflow uploads; its name column feeds the element guess |
| `utah/` | `rpm -qa` and the owner of every user-visible file inside `ghcr.io/projectbluefin/utah:testing` |
| `GAP.md` | generated: Dakota's user-visible files Utah lacks, grouped by item |
| `triage.toml` | hand-maintained: a status for every item, and an issue or reason |

"User-visible" means desktop entries, autostarts, sessions, systemd units,
and `/usr/bin` or `/usr/sbin` commands. A file counts as a gap only when Utah
has no file at the same path (`/usr/sbin` and `/usr/bin` are merged on both
images), whatever package owns it. An item is the guessed element, so
`libvirtd`, `virtqemud.service` and `virsh` all land under `libvirt`; a file
no element name matches is its own item, keyed by path. The guess takes the
longest element name that equals or prefixes the file name up to a
separator, plus a short alias table in `scripts/image-baseline.py` for
families such as `aa-*` (apparmor) or `virt*d` (libvirt).

Structural differences (bootloader, security framework, swap, shells, service
policy, ...) are tracked under the Dakota parity epic, #1236; `triage.toml`
points each matching item at the child issue.

The `surface.tsv` snapshots are only as current as the last measurement run:
widening the extract glob list does not change the committed files until
`just baselines` runs (needs podman) or `.github/workflows/image-baselines.yml`
is dispatched.

- `just baselines` refreshes Dakota's element list, re-measures both images
  (needs podman and gh) and rewrites `GAP.md`.
- `just check` fails when an item has no entry in `triage.toml`, and reports
  entries that are no longer gaps.
- `.github/workflows/image-baselines.yml` re-measures weekly and opens a pull request.
