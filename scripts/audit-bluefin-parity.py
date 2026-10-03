#!/usr/bin/env python3
"""Partition every Bluefin package Utah lacks by where it could come from.

The 2026-09-30 bare-metal audit (#382) ran by hand:
  rpm -qa both images, comm the gap, then dnf repoquery each name against
  Hummingbird and the pinned factory to learn which source -- if any -- could
  carry it. That shape is what this script exists to repeat. It is local-only
  by design: a CI job would need to mount the factory repository the audit
  resolves names against, and the script needs the same mount shape the image
  transaction uses, so the audit reads from packages/utah.toml, packages/*.repo
  and Containerfile the same way scripts/check-repo-availability.py does for
  `just check-repos`.

Three partitions, named for where a fix would land:

  hummingbird-available    resolves against Hummingbird today; moving the name
                           into packages/utah.toml [parity] would close the gap
                           the moment Hummingbird merges the rebuild it has been
                           waiting on (see packages/utah.toml header comments
                           for the dependency-graph reasons each name sits in
                           [unavailable] today).
  factory-built            absent from Hummingbird, present in the pinned
                           factory repository. The factory already builds what we
                           need; this is a manifest gap, not a factory gap.
                           Move the name into [parity] (or [hardware], etc.).
  nowhere                  neither repository provides it. A factory recipe
                           must be added first; until then the entry belongs in
                           [unavailable] with a tracking issue.

The script also writes baselines/audit-baseline.json so the gap can only grow
deliberately. `just audit-bluefin-parity --check` compares against that
baseline and fails when any partition grows.

Example
  just audit-bluefin-parity               # partition, print, do not write
  just audit-bluefin-parity --write      # partition, print, record baseline
  just audit-bluefin-parity --check      # partition, compare to baseline, fail
                                         # on growth
  just check-audit-parity --ref=HEAD     # same, with an unpinned Bluefin ref
  python3 scripts/audit-bluefin-parity.py check --ref HEAD   # direct invocation
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import re
import sys
import tarfile
import tempfile
import tomllib
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTAINERFILE = ROOT / "Containerfile"
UTAH_TOML = ROOT / "packages/utah.toml"
PARITY_REF = ROOT / "packages/.bluefin-parity-ref"
BASELINE = ROOT / "baselines/audit-baseline.json"

# Only one enabled repository exposes a non-OCI baseurl the script has to
# read -- Hummingbird's. The factory is pulled from its pinned ghcr.io
# reference (see Containerfile ARGs).
HUMMINGBIRD_REPO_FILES = (ROOT / "packages/hummingbird.repo",)


# ---------- Inputs ---------------------------------------------------------


def pinned_inputs(containerfile: Path) -> tuple[str, str]:
    """Return (base, factory OCI reference) the Containerfile pins.

    Pulled from Containerfile ARGs because the audit must resolve against the
    same pinned repository the install transaction uses; a Pages name lookup
    or a different tag cannot stand in for the digest the image actually
    mounts.
    """
    args = dict(re.findall(r"^ARG ([A-Z_]+)=(\S+)$", containerfile.read_text(), re.M))
    base = args["BASE_IMAGE"]
    packages = f"{args['PACKAGE_IMAGE']}@{args['PACKAGE_IMAGE_SHA']}"
    for image in (base, packages):
        if not re.fullmatch(r"[a-zA-Z0-9./:_-]+@sha256:[0-9a-f]{64}", image):
            raise ValueError(f"Expected a digest-pinned image, got {image!r}")
    return base, packages


def bluefin_manifest(ref: str) -> str:
    """Fetch projectbluefin/bluefin's package manifest at the pinned ref.

    The default ref is the SHA in packages/.bluefin-parity-ref; an explicit
    override exists for re-running the audit against a Bluefin candidate
    before its pins are bumped. Either way, the manifest is read once and
    parsed as TOML, exactly as packages/bluefin.toml is.
    """
    url = f"https://raw.githubusercontent.com/projectbluefin/bluefin/{ref}/build_files/packages/base.toml"
    with urllib.request.urlopen(url, timeout=120) as response:
        return response.read().decode()


def utah_manifest() -> dict:
    """Parse packages/utah.toml and partition its sections by role.

    Every name in [gnome], [parity], [hardware], [services] and [build] is
    installed; every name in [unavailable] is a documented gap. Bluefin
    names that appear in [unavailable] are deliberate omissions, so they
    are excluded from the audit the same way installed names are.
    """
    data = tomllib.loads(UTAH_TOML.read_text())
    installed: set[str] = set()
    for section in ("gnome", "parity", "hardware", "services", "build"):
        installed.update(data.get(section, {}).get("packages", []))
    unavailable = set(data.get("unavailable", {}).get("packages", []))
    return {"installed": installed, "unavailable": unavailable, "raw": data}


def bluefin_packages(manifest_text: str) -> set[str]:
    """The union of every package Bluefin's manifest names.

    Every section except [multimedia_overrides] (which is a swap of a name
    Fedora already ships) and [excluded] (removals) is in this set; the audit
    treats Bluefin's contract as the union, because `multimedia_overrides`
    records the same names that are already satisfied under a different
    repository, not additional ones. See docs/skills/package-contract.md
    "multimedia_overrides are not missing packages".

    The set of sections is derived from the manifest rather than enumerated,
    so a future `[fedora_v45]` (or any per-Fedora-version section Bluefin
    adds) is picked up automatically; a hardcoded tuple of versions goes
    stale the day Fedora ships a new release and silently misses the gap.
    """
    data = tomllib.loads(manifest_text)
    names: set[str] = set()
    for section, body in data.items():
        if section == "fedora" or section.startswith("fedora_v"):
            names.update(body.get("packages", []))
    return names


# ---------- Repodata: factory OCI image ------------------------------------


def verified_bytes(raw: bytes, digest: str) -> bytes:
    if digest != "sha256:" + hashlib.sha256(raw).hexdigest():
        raise ValueError(f"Registry content does not match {digest}")
    return raw


def repository_metadata(image: str, destination: Path) -> None:
    """Pull the factory's leading repodata layer and unpack it under destination.

    The factory publishes repodata first, separately from the large RPM
    payload, so this function reads the leading layer of the manifest and
    refuses to fall back to a Pages lookup. A leading layer larger than 64 MiB
    is a publish layout bug, not a packing one, and a --repo-availability run
    must fail loud rather than quietly use a different repository.
    """
    registry, reference = image.split("/", 1)
    if registry != "ghcr.io":
        raise ValueError("The factory metadata reader currently supports ghcr.io images")
    repository, digest = reference.split("@", 1)
    query = urllib.parse.urlencode({"service": registry, "scope": f"repository:{repository}:pull"})
    with urllib.request.urlopen(f"https://{registry}/token?{query}", timeout=120) as response:
        token = json.load(response)["token"]

    def fetch(kind: str, digest: str) -> bytes:
        request = urllib.request.Request(
            f"https://{registry}/v2/{repository}/{kind}/{digest}",
            headers={"Authorization": f"Bearer {token}",
                     "Accept": "application/vnd.oci.image.manifest.v1+json"},
        )
        with urllib.request.urlopen(request, timeout=120) as response:
            return verified_bytes(response.read(), digest)

    manifest = json.loads(fetch("manifests", digest))
    layer = manifest["layers"][0]
    if layer["size"] > 64 * 1024 * 1024:
        raise ValueError("Package image lacks a small leading metadata layer; republish with repodata first")
    unpack_metadata(fetch("blobs", layer["digest"]), destination)


def unpack_metadata(raw: bytes, destination: Path) -> None:
    """Safely unpack a tar.gz repodata archive.

    Rejects absolute paths and `..` traversal; the archive is from a
    registry, so anything outside repository/repodata/ is a publish bug and
    must surface as one. The audit needs every path it extracts to land
    under the destination, so a single --absolute or .. entry aborts the
    whole pull rather than continuing with a half-extracted repository.
    """
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
        for entry in archive:
            path = Path(entry.name)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError(f"Unsafe metadata path: {entry.name}")
            if entry.isdir():
                continue
            if not entry.isfile() or path.parts[:2] != ("repository", "repodata"):
                raise ValueError(f"Unexpected entry in metadata layer: {entry.name}")
            target = destination / Path(*path.parts[1:])
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.extractfile(entry).read())
    if not (destination / "repodata/repomd.xml").is_file():
        raise ValueError("Pinned metadata layer contains no repomd.xml")


# ---------- Repodata: Hummingbird yum repository ------------------------------


def fetch_hummingbird_repodata(destination: Path) -> tuple[str, str]:
    """Fetch Hummingbird's repomd.xml and the primary.xml it names.

    The baseurl is in packages/hummingbird.repo; only the .repo file Utah
    ships is read, so a future Fedora-release switch is one grep away.
    Returns (baseurl, primary_basename) so the caller can quote the source
    in the report and locate the file parse_primary() should read. The
    primary file is written under its repomd.xml-named basename (e.g.
    `repomd.xml.primary.xml.gz`) so the gzipped-vs-plain distinction is
    visible to parse_primary() at the path level.
    """
    baseurl = ""
    for repo_file in HUMMINGBIRD_REPO_FILES:
        for line in repo_file.read_text().splitlines():
            match = re.match(r"\s*baseurl\s*=\s*(\S+)", line)
            if match:
                baseurl = match.group(1)
    if not baseurl:
        raise ValueError("Could not find a baseurl in packages/hummingbird.repo")
    baseurl = baseurl.rstrip("/") + "/"

    repomd = ET.fromstring(urllib.request.urlopen(baseurl + "repodata/repomd.xml", timeout=120).read())
    primary_path = ""
    primary_checksum = None
    for child in repomd:
        if child.tag.endswith("data") and child.attrib.get("type") == "primary":
            for location in child:
                # The local-name suffix match rejects <open-checksum>:
                # a gzipped primary.xml.gz carries both a <checksum> for
                # the compressed bytes and an <open-checksum> for the
                # uncompressed form, and we just downloaded the compressed
                # bytes. A bare endswith("checksum") match picks up the
                # open-checksum (which arrives second in iteration order)
                # and fails every fetch against a gzipped repository.
                local = location.tag.rsplit("}", 1)[-1]
                if local == "location":
                    primary_path = location.attrib["href"]
                elif local == "checksum":
                    primary_checksum = (location.attrib["type"], location.text)
            break
    if not primary_path:
        raise ValueError("Hummingbird repomd.xml has no primary data entry")

    raw = urllib.request.urlopen(baseurl + primary_path, timeout=300).read()
    if primary_checksum and primary_checksum[0] == "sha256":
        if hashlib.sha256(raw).hexdigest() != primary_checksum[1]:
            raise ValueError("Hummingbird primary.xml failed checksum")
    # Preserve the repomd.xml-named basename so the gzipped form (the
    # current Hummingbird layout: repodata/repomd.xml names primary.xml.gz)
    # stays distinguishable from the plain XML form. parse_primary() keys
    # on the basename to decide whether to decompress.
    primary_basename = Path(primary_path).name
    (destination / primary_basename).write_bytes(raw)
    return baseurl, primary_basename


# ---------- Parsing primary.xml --------------------------------------------


def parse_primary(path: Path) -> dict[str, str]:
    """Return {name: latest evr} from a repodata primary.xml.

    The audit partitions by name, so a single (name -> evr) map is the
    primary output of the script. EVR is recorded so the report prints
    `dnf list --available`-shaped evidence per name.

    Repositories publish primary.xml in either form (plain XML or gzipped);
    `primary_href()` returns the file basename, so the caller does not know
    which form it landed on. Read the file by name and decompress on the
    fly when the basename ends in .gz, mirroring how Hummingbird's
    repomd.xml names the primary data (`*-primary.xml.gz`).
    """
    raw = path.read_bytes()
    if path.name.endswith(".gz"):
        raw = gzip.decompress(raw)
    ns = ""
    root = ET.fromstring(raw)
    if root.tag.startswith("{"):
        ns = root.tag.split("}", 1)[0] + "}"

    latest: dict[str, tuple[int, str, str, str]] = {}
    for package in root.iter(ns + "package"):
        name_el = package.find(ns + "name")
        if name_el is None or name_el.text is None:
            continue
        name = name_el.text
        version = package.find(ns + "version")
        release = package.find(ns + "release")
        epoch_el = package.find(ns + "epoch")
        arch_el = package.find(ns + "arch")
        evr = (epoch_el.text if epoch_el is not None and epoch_el.text else "0",
               version.text if version is not None else "",
               release.text if release is not None else "",
               arch_el.text if arch_el is not None else "x86_64")
        # yum primary.xml orders packages so the same name appears once, but
        # we keep the latest rather than the last so an out-of-order entry
        # does not regress the comparison.
        current = latest.get(name)
        if current is None or evr > current:
            latest[name] = evr
    out: dict[str, str] = {}
    for name, (epoch, version, release, arch) in latest.items():
        if epoch and epoch != "0":
            out[name] = f"{epoch}:{version}-{release}.{arch}"
        else:
            out[name] = f"{version}-{release}.{arch}"
    return out


def dnf_list_line(name: str, evr: str) -> str:
    """Format an evr into the three columns `dnf list --available` prints.

    `dnf list available` uses N/V-R.A for "name / version-release.arch" with a
    space and version. The audit prints the same so a name that moves partitions
    reads as evidence, not as a free-form string.
    """
    arch = evr.rsplit(".", 1)[-1]
    body = evr[: -len("." + arch)] if "." in evr else evr
    return f"{name}.{arch}\t{body}"


# ---------- Partition ------------------------------------------------------


def gap_names(bluefin: set[str], utah: dict) -> list[str]:
    """Sorted Bluefin names Utah neither installs nor lists as unavailable.

    Sorted, so two consecutive audits produce byte-stable diffs and the
    recorded baseline is meaningful as a JSON object rather than a JSON
    list-of-keys that reorders every run.
    """
    satisfied = utah["installed"] | utah["unavailable"]
    return sorted(bluefin - satisfied)


def partition(gap: list[str], hummingbird: dict[str, str],
              factory: dict[str, str]) -> dict[str, list[str]]:
    """Sort gap names by which repository can supply them.

    The order of the check matters: a name that has migrated to
    Hummingbird's rebuild since the last audit is still in `factory`, but
    we want to point the operator at the smallest move that closes the
    gap. Hummingbird is the smallest target (priority 10, no recipe needed)
    so we attribute a name there before checking the factory.
    """
    out = {"hummingbird-available": [], "factory-built": [], "nowhere": []}
    for name in gap:
        if name in hummingbird:
            out["hummingbird-available"].append(name)
        elif name in factory:
            out["factory-built"].append(name)
        else:
            out["nowhere"].append(name)
    return out


# ---------- Report and baseline -------------------------------------------


def render_report(parts: dict[str, list[str]], hummingbird: dict[str, str],
                  factory: dict[str, str], ref: str, baseurl: str,
                  factory_ref: str) -> str:
    """Build the human-readable partition report.

    Per-name evidence is the (name, evr) pair `dnf list --available` would
    have printed for that name in the matching repository. Names in the
    "nowhere" partition have no EVR -- that is the whole point.
    """
    lines = [
        "# Bluefin-vs-Utah image parity audit",
        "",
        f"- Bluefin ref: `{ref}`",
        f"- Hummingbird source: `{baseurl}`",
        f"- Factory OCI reference: `{factory_ref}`",
        "",
        f"## hummingbird-available ({len(parts['hummingbird-available'])})",
        "Resolves against Hummingbird's own repository today. "
        "Move the name into packages/utah.toml [parity] (or [hardware], etc.) "
        "to install it on the next build.",
        "",
        "| name | Hummingbird version",
        "| --- | --- |",
    ]
    for name in parts["hummingbird-available"]:
        evr = hummingbird.get(name, "?")
        lines.append(f"| {name} | `{dnf_list_line(name, evr)}` |")

    lines.extend([
        "",
        f"## factory-built ({len(parts['factory-built'])})",
        "Absent from Hummingbird, present in the pinned factory repository. "
        "The factory already builds what we need; this is a manifest gap.",
        "",
        "| name | factory version",
        "| --- | --- |",
    ])
    for name in parts["factory-built"]:
        evr = factory.get(name, "?")
        lines.append(f"| {name} | `{dnf_list_line(name, evr)}` |")

    lines.extend([
        "",
        f"## nowhere ({len(parts['nowhere'])})",
        "Neither repository provides it. A factory recipe must land first; "
        "until then the name belongs in [unavailable] with a tracking issue.",
        "",
    ])
    if parts["nowhere"]:
        lines.append("| name |")
        lines.append("| --- |")
        for name in parts["nowhere"]:
            lines.append(f"| {name} |")
    else:
        lines.append("(empty)")

    return "\n".join(lines) + "\n"


def baseline_record(parts: dict[str, list[str]], ref: str, factory_ref: str,
                    baseurl: str) -> dict:
    """The baseline file is a partition map keyed by the source it audits.

    A flat list of names lets two consecutive audits diff as JSON objects
    rather than as text: the same name in the same partition is silent, the
    same name in a different partition is the kind of migration the audit
    is meant to catch. `meta` records what was read so a stale baseline
    surfaces as such, not as a silent "no growth" verdict.
    """
    return {
        "ref": ref,
        "factory_ref": factory_ref,
        "hummingbird_baseurl": baseurl,
        "hummingbird-available": sorted(parts["hummingbird-available"]),
        "factory-built": sorted(parts["factory-built"]),
        "nowhere": sorted(parts["nowhere"]),
    }


STALE_PREFIX = "stale baseline: "


def compare_to_baseline(parts: dict[str, list[str]], baseline: dict, ref: str,
                        factory_ref: str, baseurl: str) -> list[str]:
    """Diff each partition against the recorded baseline.

    A name migrating between partitions is celebrated as a rebuild
    landing, not flagged as drift. The "moved elsewhere" set per partition
    is the names in `new` minus the names in `old`, intersected with the
    set of names that used to live in any other partition; these are
    silent. The remaining new names -- ones that did not exist in the
    baseline at all -- are the regression set, and the gate fails on them.

    Shrinks are a no-op: a name dropping from a partition because the
    gap closed (move to [parity] / [hardware] / etc.) is the operator's
    intent, not a regression.

    The baseline records the `ref` and `factory_ref` it was captured
    against (see `baseline_record`), as well as `hummingbird_baseurl`. Those are
    what make the partition lists comparable: two audits only describe the
    same package set when they were read against the same Bluefin ref, factory
    pin, and repo URL. A bump that leaves the partition lists unchanged would
    otherwise read as "no growth" and pass silently -- the baseline is
    stale, it is not confirming the debt. Surface that mismatch first, so
    the operator rewrites the baseline against the new ref instead of
    trusting a verdict captured under a different one.
    """
    msgs: list[str] = []

    # A ref or baseurl mismatch means the partition lists are not comparable to
    # the baseline at all, regardless of whether they grew. Report it before
    # the partition diff so the stale-baseline verdict is never masked by
    # (or buried under) a growth report.
    ref_changes = (
        ("ref", ref, baseline.get("ref")),
        ("factory_ref", factory_ref, baseline.get("factory_ref")),
        ("hummingbird_baseurl", baseurl, baseline.get("hummingbird_baseurl")),
    )
    for key, current, recorded in ref_changes:
        if recorded is not None and recorded != current:
            msgs.append(
                f"{STALE_PREFIX}{key} changed from {recorded!r} to {current!r}"
            )

    old_names_by_partition: dict[str, set[str]] = {
        partition: set(baseline.get(partition, []))
        for partition in ("hummingbird-available", "factory-built", "nowhere")
    }
    all_old = set().union(*old_names_by_partition.values())

    for partition_name in ("hummingbird-available", "factory-built", "nowhere"):
        old = old_names_by_partition[partition_name]
        new = set(parts[partition_name])
        added = new - old
        # Migrations (the name used to live in some other partition) are
        # silent; brand-new names that did not exist anywhere in the
        # baseline are the regression the gate must fail on.
        regressions = sorted(name for name in added if name not in all_old)
        if regressions:
            msgs.append(f"{partition_name}: +{len(regressions)} {regressions}")
    return msgs


# ---------- Subcommand glue ------------------------------------------------


def load_baseline() -> dict:
    if not BASELINE.is_file():
        return {}
    try:
        return json.loads(BASELINE.read_text())
    except json.JSONDecodeError as error:
        raise ValueError(f"{baseline_path()} is not valid JSON: {error}")


def baseline_path() -> str:
    """Path of the baseline file, relative to the repo root when possible.

    Display-only; tests substitute a path outside ROOT, and `relative_to`
    raises on a path that escapes. Fall back to the absolute path so the
    gate's error message stays informative when that happens.
    """
    try:
        return str(BASELINE.relative_to(ROOT))
    except ValueError:
        return str(BASELINE)


def primary_href(repomd: Path) -> str:
    """Locate the primary data path relative to the repomd file's parent.

    yum primary.xml / primary.xml.gz is published alongside repomd.xml
    (both at `<repo>/repodata/`), so the on-disk file is always in the
    same directory as repomd.xml regardless of what the location href
    says. Use the basename so a move between two layout conventions
    (e.g., repodata/repodata/primary.xml.gz vs. repodata/primary.xml.gz)
    does not silently miss the file.
    """
    repomd_doc = ET.fromstring(repomd.read_bytes())
    for data in repomd_doc:
        if data.tag.endswith("data") and data.attrib.get("type") == "primary":
            for child in data:
                if child.tag.endswith("location"):
                    return Path(child.attrib["href"]).name
    raise ValueError("repodata/repomd.xml has no primary data entry")


# Ref validator kept as a separate function so the tests exercise the
# exact regex fetch_partition() applies, instead of duplicating the
# pattern in the test file. Tests that previously inlined the regex
# cannot catch a drift in this one.
BLUEFIN_REF_PATTERNS = (
    re.compile(r"[0-9a-f]{40}"),
    re.compile(r"^(?!.*\.\.)[A-Za-z0-9._/-]+$"),
)


def validate_bluefin_ref(ref: str) -> None:
    """Raise ValueError unless `ref` is a Bluefin commit SHA, branch, or tag.

    A Bluefin ref is one of:

    - a 40-character hex string (commit SHA), or
    - a ref name that matches `^(?!.*\\.\\.)[A-Za-z0-9._/-]+$` (branch
      or tag). The `..` lookahead rejects path traversal and shell
      metacharacters; the regex restricts the alphabet to what git and
      GitHub accept for ref names.

    Anything else raises ValueError with the offending ref quoted. The
    caller decides whether to fail the audit, fall back to the pinned
    SHA, or surface it as a usage error.
    """
    if not any(pattern.fullmatch(ref) for pattern in BLUEFIN_REF_PATTERNS):
        raise ValueError(f"Bluefin ref {ref!r} is neither a commit SHA nor a branch/tag name")


def fetch_partition(args) -> tuple:
    """Run the audit's expensive pieces once and return everything callers need.

    Pulls the pinned factory OCI repodata and Hummingbird's repodata, then
    partitions every Bluefin name Utah lacks. The tuple is large because
    both `run` and `check` want all of it; computing the partition twice
    would double the network and the OCI blob hash work, so both
    subcommands go through here.
    """
    ref = args.ref or PARITY_REF.read_text().strip()
    validate_bluefin_ref(ref)
    bluefin_text = bluefin_manifest(ref)
    utah = utah_manifest()
    factory_ref = pinned_inputs(CONTAINERFILE)[1]
    gap = gap_names(bluefin_packages(bluefin_text), utah)

    with tempfile.TemporaryDirectory(prefix="utah-audit-") as tmp:
        root = Path(tmp)
        repository_metadata(factory_ref, root / "factory")
        repomd = root / "factory/repodata/repomd.xml"
        factory_index_path = repomd.parent / primary_href(repomd)
        factory = parse_primary(factory_index_path)
        baseurl, hummingbird_basename = fetch_hummingbird_repodata(root)
        hummingbird = parse_primary(root / hummingbird_basename)

    parts = partition(gap, hummingbird, factory)
    return ref, parts, hummingbird, factory, factory_ref, baseurl


def cmd_run(args) -> int:
    """Partition, print, and (optionally) record the baseline.

    Default mode is report-only: nothing is written. `--write` writes a
    fresh baseline after the partition, which is how a maintainer commits
    the new starting state of the debt after closing gaps.
    """
    ref, parts, hummingbird, factory, factory_ref, baseurl = fetch_partition(args)
    print(render_report(parts, hummingbird, factory, ref, baseurl, factory_ref))

    if args.write:
        BASELINE.parent.mkdir(parents=True, exist_ok=True)
        BASELINE.write_text(json.dumps(
            baseline_record(parts, ref, factory_ref, baseurl), indent=2, sort_keys=True
        ) + "\n")
        print(f"Wrote {baseline_path()}", flush=True)
    return 0


def cmd_check(args) -> int:
    """Partition and fail on partition growth or a stale baseline.

    The check is silent on a clean run (no growth, baseline captured
    against the current refs). On growth it prints which partitions grew,
    by how much, and which names; the operator then either runs `--write`
    to commit the new debt or files the issue that closed the gap in the
    other direction. When the baseline was captured against a different
    Bluefin ref or factory pin, the partition lists are not comparable at
    all, so the failure is reported as a stale baseline and the only
    remedy is to rewrite it with `--write` -- saying "partitions grew"
    there would contradict the verdict, since nothing grew.
    """
    baseline = load_baseline()
    if not baseline:
        print(f"ERROR: {baseline_path()} does not exist; "
              "run `just audit-bluefin-parity --write` once to record the first baseline",
              file=sys.stderr)
        return 2

    ref, parts, _, _, factory_ref, baseurl = fetch_partition(args)
    msgs = compare_to_baseline(parts, baseline, ref, factory_ref, baseurl)
    if msgs:
        for msg in msgs:
            print(f"ERROR: {msg}", file=sys.stderr)
        stale = any(msg.startswith(STALE_PREFIX) for msg in msgs)
        growth = any(not msg.startswith(STALE_PREFIX) for msg in msgs)
        if stale and growth:
            trailer = (f"{baseline_path()} is stale and partitions grew past it; "
                       "fix the underlying gap or run "
                       "`just audit-bluefin-parity --write` to re-record the "
                       "baseline against the current refs")
        elif stale:
            trailer = (f"{baseline_path()} was captured against different refs; "
                       "run `just audit-bluefin-parity --write` to re-record it "
                       "before the gate can confirm the debt")
        else:
            trailer = (f"partitions grew past {baseline_path()}; "
                       "either fix the underlying gap or run "
                       "`just audit-bluefin-parity --write` to commit the new debt")
        print(f"ERROR: {trailer}", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="partition the gap and print the report")
    run.add_argument("--write", action="store_true",
                     help="record the new partition to baselines/audit-baseline.json")
    run.add_argument("--ref", default=None,
                     help="Bluefin ref to audit (commit SHA, branch or tag); defaults to .bluefin-parity-ref")
    check = sub.add_parser("check", help="fail when a partition grew past the baseline")
    check.add_argument("--ref", default=None,
                       help="Bluefin ref to audit (commit SHA, branch or tag); defaults to .bluefin-parity-ref")
    args = parser.parse_args()
    if args.cmd == "run":
        return cmd_run(args)
    return cmd_check(args)


if __name__ == "__main__":
    raise SystemExit(main())
