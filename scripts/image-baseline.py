#!/usr/bin/env python3
"""Measure Utah against what Bluefin and Dakota actually ship.

packages/bluefin.toml is the list of packages Bluefin's build *adds*. Everything
Bluefin inherits from its Fedora base -- gnome-initial-setup, wpa_supplicant,
plymouth, cups -- never appears in it, so Utah's RPM contract could pass while
an installed Utah had no Wi-Fi and no first-boot account setup. The baselines
here come from the published images instead:

  extract IMAGE DIR   rpm -qa, plus which package owns every user-visible file
                      (desktop entries, autostarts, sessions, systemd units,
                      /usr/bin), from inside IMAGE. Needs podman.
  dakota RUN DIR      element list from the SPDX SBOM that Dakota's publish
                      workflow uploads (artifact sbom-dakota). Dakota is built
                      with BuildStream, so it has no RPM database to read.
  gap                 write baselines/GAP.md: Bluefin packages Utah does not
                      install *and* whose user-visible files Utah lacks.
                      Matching by file, not only by name, keeps renamed
                      packages (coreutils-single, ...) out of the report.
  check               fail when a gap is not listed in baselines/triage.toml,
                      so a new gap cannot land silently. Listed gaps pass: the
                      file is the tracked parity debt, each entry with a status.

Refresh all snapshots with `just baselines`.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import tomllib
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "baselines"
TRIAGE = BASE / "triage.toml"
STATUSES = {"planned", "waived", "unavailable", "untriaged"}

EXTRACT = r"""
set -euo pipefail
rpm -qa --qf '%{NAME}\t%{VERSION}-%{RELEASE}\n' | sort -u > /out/rpms.tsv
for pat in /usr/share/applications/*.desktop /etc/xdg/autostart/*.desktop \
           /usr/share/gdm/greeter/autostart/*.desktop /usr/share/wayland-sessions/*.desktop \
           /usr/share/gnome-session/sessions/*.session \
           /usr/lib/systemd/system/*.service /usr/lib/systemd/system/*.socket \
           /usr/lib/systemd/system/*.timer /usr/lib/systemd/user/*.service \
           /usr/lib/systemd/user/*.socket /usr/bin/* /usr/sbin/*; do
  for f in $pat; do
    [ -e "$f" ] || continue
    o=$(rpm -qf --qf '%{NAME}\n' "$f" 2>/dev/null | head -1) || o=""
    case "$o" in ""|*"not owned"*) o="(unowned)";; esac
    printf '%s\t%s\n' "$o" "$f"
  done
done | sort -u > /out/surface.tsv
"""

# Order matters: the first match names the kind in the report.
KINDS = (
    ("session", ("/wayland-sessions/", "/gnome-session/sessions/")),
    ("app", ("/usr/share/applications/",)),
    ("autostart", ("/autostart/",)),
    ("user unit", ("/systemd/user/",)),
    ("system unit", ("/systemd/system/",)),
    ("command", ("/usr/bin/", "/usr/sbin/")),
)


def kind(path: str) -> str:
    return next(k for k, marks in KINDS if any(m in path for m in marks))


def read_tsv(path: Path) -> list[list[str]]:
    return [line.split("\t") for line in path.read_text().splitlines()
            if line and not line.startswith("#")]


def extract(image: str, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    digest = subprocess.run(
        ["podman", "image", "inspect", "--format", "{{.Digest}}", image],
        capture_output=True, text=True).stdout.strip()
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "x.sh"
        script.write_text(EXTRACT)
        subprocess.run(["podman", "run", "--rm", "--entrypoint", "/bin/bash",
                        "-v", f"{script}:/x.sh:ro,z", "-v", f"{tmp}:/out:z",
                        image, "/x.sh"], check=True)
        for name in ("rpms.tsv", "surface.tsv"):
            (out / name).write_text((Path(tmp) / name).read_text())
    (out / "image.txt").write_text(f"{image.split('@')[0]}@{digest}\n" if digest else f"{image}\n")


def dakota(run: str, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(["gh", "run", "download", run, "-R", "projectbluefin/dakota",
                        "-n", "sbom-dakota", "-D", tmp], check=True)
        sbom = json.loads((Path(tmp) / "dakota.sbom.json").read_text())
    rows = set()
    for pkg in sbom["packages"]:
        ref = pkg["SPDXID"].removeprefix("SPDXRef-")
        if not ref.endswith(".bst"):
            continue  # "-N" entries are the sources of an element, not elements
        rows.add((ref, pkg.get("name") or "", pkg.get("versionInfo") or ""))
    lines = ["# element\tname\tversion"] + ["\t".join(r) for r in sorted(rows)]
    (out / "elements.tsv").write_text("\n".join(lines) + "\n")
    (out / "source.txt").write_text(
        f"projectbluefin/dakota publish.yml run {run}, artifact sbom-dakota\n")


def gaps() -> dict[str, list[str]]:
    utah_pkgs = {r[0] for r in read_tsv(BASE / "utah/rpms.tsv")}
    utah_paths = {r[1] for r in read_tsv(BASE / "utah/surface.tsv")}
    out: dict[str, list[str]] = defaultdict(list)
    for pkg, path in read_tsv(BASE / "bluefin/surface.tsv"):
        if pkg != "(unowned)" and pkg not in utah_pkgs and path not in utah_paths:
            out[pkg].append(path)
    return dict(sorted(out.items()))


def triage() -> dict[str, dict]:
    data = tomllib.loads(TRIAGE.read_text()) if TRIAGE.exists() else {}
    return data.get("package", {})


def dakota_has(pkg: str) -> str:
    """Name of a Dakota element whose basename matches pkg, if any."""
    path = BASE / "dakota/elements.tsv"
    if not path.exists():
        return ""
    # SPDX ids flatten the element path, so "core/gnome-initial-setup.bst" in
    # the gnome-build-meta junction arrives as
    # "gnome-build-meta.bst-core-gnome-initial-setup.bst". Splitting on "-"
    # cannot recover a hyphenated name; match the suffix instead.
    for element, *_ in read_tsv(path):
        if element == f"{pkg}.bst" or element.endswith(f"-{pkg}.bst"):
            return element
    return ""


def write_report() -> None:
    found, notes = gaps(), triage()
    by_status: dict[str, list[str]] = defaultdict(list)
    for pkg in found:
        by_status[notes.get(pkg, {}).get("status", "not in triage.toml")].append(pkg)
    lines = [
        "# Utah vs Bluefin: user-visible gaps",
        "",
        "Generated by `scripts/image-baseline.py gap`; do not edit.",
        "",
        f"- Bluefin: `{(BASE / 'bluefin/image.txt').read_text().strip()}`",
        f"- Utah: `{(BASE / 'utah/image.txt').read_text().strip()}`",
        f"- Dakota: {(BASE / 'dakota/source.txt').read_text().strip()}",
        "",
        f"{len(found)} Bluefin packages are not installed in Utah and own "
        "user-visible files Utah lacks.",
        "",
        "| status | count |", "| --- | ---: |",
        *(f"| {s} | {len(p)} |" for s, p in sorted(by_status.items())),
        "",
        "| package | status | what is missing | in Dakota | note |",
        "| --- | --- | --- | --- | --- |",
    ]
    for pkg, paths in found.items():
        entry = notes.get(pkg, {})
        kinds: dict[str, int] = defaultdict(int)
        for p in paths:
            kinds[kind(p)] += 1
        what = ", ".join(f"{n} {k}{'s' if n > 1 else ''}" for k, n in kinds.items())
        note = entry.get("reason", "")
        if entry.get("issue"):
            note = f"{note} ({entry['issue']})".strip()
        lines.append(f"| {pkg} | {entry.get('status', '**new**')} | {what} | "
                     f"{dakota_has(pkg) or ''} | {note} |")
    (BASE / "GAP.md").write_text("\n".join(lines) + "\n")


def check() -> int:
    found, notes = gaps(), triage()
    bad = [f"{p}: status {e.get('status')!r} is not one of {sorted(STATUSES)}"
           for p, e in notes.items() if e.get("status") not in STATUSES]
    new = [p for p in found if p not in notes]
    if new:
        bad.append("Bluefin packages missing from Utah and not in baselines/triage.toml: "
                   + ", ".join(new))
    for msg in bad:
        print(f"ERROR: {msg}", file=sys.stderr)
    if not bad:
        closed = [p for p in notes if p not in found]
        print(f"{len(found)} gaps, all triaged"
              + (f"; no longer gaps, drop from triage.toml: {', '.join(closed)}" if closed else ""))
    return 1 if bad else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("extract"); e.add_argument("image"); e.add_argument("out", type=Path)
    d = sub.add_parser("dakota"); d.add_argument("run"); d.add_argument("out", type=Path)
    sub.add_parser("gap"); sub.add_parser("check")
    args = parser.parse_args()
    if args.cmd == "extract":
        extract(args.image, args.out)
    elif args.cmd == "dakota":
        dakota(args.run, args.out)
    elif args.cmd == "gap":
        write_report()
    else:
        return check()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
