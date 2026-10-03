# Image baselines

What Bluefin and Dakota actually ship, measured from the published images,
so Utah's parity is checked against reality rather than against a manifest.

`packages/bluefin.toml` lists only the packages Bluefin's build *adds*.
Everything Bluefin inherits from its Fedora base (gnome-initial-setup,
wpa_supplicant, plymouth, cups) never appears there. Utah's RPM contract
therefore passed while an installed Utah had no Wi-Fi and no first-boot
account setup.

| path | source |
| --- | --- |
| `bluefin/` | `rpm -qa` and the owner of every user-visible file inside `ghcr.io/ublue-os/bluefin:stable` |
| `utah/` | the same, inside `ghcr.io/projectbluefin/utah:testing` |
| `dakota/elements.tsv` | the SPDX SBOM Dakota's publish workflow uploads (BuildStream, no RPM database) |
| `GAP.md` | generated: Bluefin packages Utah lacks whose files Utah also lacks |
| `triage.toml` | hand-maintained: a status and area for every gap |

"User-visible" means desktop entries, autostarts, sessions, systemd units,
`/usr/bin` or `/usr/sbin` commands, and the Bluefin firefox-config defaults
under `/usr/share/ublue-os/firefox-config/` (copied onto the running system by
`99-flatpaks.sh`). A package counts as a gap only when it is
missing by name **and** its files are missing, so renamed packages
(`coreutils` vs `coreutils-single`) do not show up.

The `surface.tsv` snapshots are only as current as the last measurement run:
widening the EXTRACT glob list does not change the committed files until
`just baselines` runs (needs podman) or `.github/workflows/image-baselines.yml`
is dispatched. A change that adds a path therefore lands in two steps -- the
glob in `scripts/image-baseline.py`, then the next re-measure that writes the
new rows into `bluefin/surface.tsv` and `utah/surface.tsv`.

- `just baselines` re-measures all three (needs podman and gh) and rewrites `GAP.md`.
- `just check` fails when a gap has no entry in `triage.toml`.
- `.github/workflows/image-baselines.yml` re-measures weekly and opens a pull request.
