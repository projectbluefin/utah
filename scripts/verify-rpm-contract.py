#!/usr/bin/env python3
"""Assert that Utah actually contains its Bluefin and GNOME 51 RPM contracts.

Beyond package presence, this is the supply-chain attestation for issue #21:
GNOME packages carry the promised major version and an approved factory
(`.bfin`) or Hummingbird (`.hum`) identity; parity packages cannot silently
resolve from an unapproved Fedora repository; the composed image exposes only
the runtime repositories the manifest allows; and the resolved
package-origin/NEVRA set is retained as a report with build provenance.

`--check` validates the manifest itself off-image: the `.repo` files in
`packages/` may name only the repositories the manifest allows. The on-image
run applies the same allowlist to the composed image's runtime RPM
repositories -- dnf5's default reposdir paths (#454, #513).

Mirrors assert_packages_present from projectbluefin/bluefin's
build_files/shared/package-lib.sh: name every missing package, once.
"""

from __future__ import annotations

import argparse
import configparser
import datetime
import json
import os
import re
import shlex
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

# The NVIDIA userspace no longer arrives as RPMs. UBlue's akmods bundle used to
# supply nvidia-driver, nvidia-driver-cuda and nvidia-container-toolkit, but it
# publishes nothing for Hummingbird's kernel, so install-nvidia.sh builds the
# open module from NVIDIA's own source and installs the matching userspace from
# the same payload. Those files are what the image needs; the RPM names were
# only ever how they happened to arrive.
#
# nvidia-container-toolkit still arrives as an RPM, from NVIDIA's own repository
# rather than from the akmods bundle, so it is asserted by name. It was recorded
# here as a real loss on the reasoning that the bundle was unusable; the bundle
# was one source, not the only one.
NVIDIA_PACKAGES = (
    "nvidia-container-toolkit",
)

CONTRACT_PATH = "/usr/share/utah/contract.txt"
DEFAULT_REPORT_DIR = "/usr/share/utah"
# The factory pin travels on the image as a stamp in the repository file, which
# is what the report quotes: it says which package factory the NEVRAs came from
# without needing a build argument plumbed through every stage.
FACTORY_REPO_PATH = "/etc/yum.repos.d/utah-packages.repo"
# Where the composed image's runtime RPM repositories live. --check works
# against the source repo files in packages/; the on-image run scans dnf5's
# default reposdir paths so repo files shipped by the Hummingbird base image are
# subject to the same allowlist as the ones Utah itself copies in (#454, #513).
# dnf5 loads every one of these when it resolves packages, so scanning only
# /etc/yum.repos.d left a repo file the base ships in another default reposdir
# enabled at runtime yet invisible to the gate (#513).
# dnf5 also reads repo-override drop-ins from two override directories: a
# system override (/etc/dnf/repos.override.d) and a distribution override
# (/usr/share/dnf5/repos.override.d). A .repo file there can set enabled=/baseurl=
# on a repo id defined in the scanned dirs, so a base-image override could
# re-enable a repo the gate observed as disabled. Scan those too (#524).
RUNTIME_REPOS_DIRS: tuple[Path, ...] = (
    Path("/etc/yum.repos.d"),
    Path("/etc/distro.repos.d"),
    Path("/usr/share/dnf5/repos.d"),
    Path("/etc/dnf/repos.override.d"),
    Path("/usr/share/dnf5/repos.override.d"),
)
FACTORY_PIN_RE = re.compile(r"^# factory-pin: (?P<digest>\S+)\s*$", re.MULTILINE)

DISABLED_VALUES: frozenset[str] = frozenset({"0", "false", "no", "off"})


def section(overlay: Path, name: str, key: str = "packages") -> list[str]:
    """A named package list from an overlay manifest, in the order written.

    `key` selects which list in the section to read, so a section can declare
    more than one bucket of names ([factory] declares `packages` and `parity`).
    """
    data = tomllib.loads(overlay.read_text(encoding="utf-8"))
    if name not in data or key not in data[name]:
        return []
    return list(data[name][key])


def is_installed(package: str) -> bool:
    """Whether an RPM named `package` is installed (rpm -q exit status)."""
    return subprocess.run(["rpm", "-q", package], capture_output=True).returncode == 0


def is_repo_enabled(enabled_val: str) -> bool:
    """Normalize boolean repository enabled semantics, failing closed on unknown values."""
    return enabled_val.strip().lower() not in DISABLED_VALUES


def determine_origin(pkg: str, release: str) -> str:
    """Classify a package's origin from its release identity, not its name.

    The NVIDIA case is decided by membership in NVIDIA_PACKAGES rather than by
    searching for "nvidia" inside the release string, which a coincidental
    rebuild could trip.
    """
    if ".bfin" in release:
        return "factory"
    if ".hum" in release:
        return "hummingbird"
    if pkg in NVIDIA_PACKAGES:
        return "nvidia"
    if ".fc" in release:
        return "fedora"
    return "unknown"


def query_packages(packages: list[str]) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Query rpm for NEVRA attributes of requested packages.

    rpm -q prints one line per installed copy of a name; a multilib pair is two
    installs of one package. Every copy is retained so neither escapes the
    release-identity checks.
    """
    if not packages:
        return {}, []
    try:
        res = subprocess.run(
            ["rpm", "-q", "--qf", "%{NAME}|%{EPOCHNUM}|%{VERSION}|%{RELEASE}|%{ARCH}\n", *packages],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        res = None

    installed: dict[str, dict[str, Any]] = {}
    if res and res.stdout:
        for line in res.stdout.splitlines():
            line = line.strip()
            if not line or "|" not in line:
                continue
            parts = line.split("|")
            if len(parts) != 5:
                continue
            name, epoch, version, release, arch = parts
            nevra = (
                f"{name}-{version}-{release}.{arch}"
                if epoch in ("", "0", "(none)")
                else f"{name}-{epoch}:{version}-{release}.{arch}"
            )
            install = {
                "name": name, "epoch": epoch, "version": version,
                "release": release, "arch": arch, "nevra": nevra,
                "origin": determine_origin(name, release),
            }
            existing = installed.get(name)
            if existing is None:
                installed[name] = {**install, "installs": [install]}
            else:
                existing["installs"].append(install)
    missing = [p for p in packages if p not in installed]
    return installed, missing


def installs_of(info: dict[str, Any]) -> list[dict[str, str]]:
    """Every installed copy recorded for a package name, including multilib pairs."""
    return info.get("installs") or [info]


def verify_gnome_contract(
    gnome_packages: list[str],
    installed: dict[str, dict[str, Any]],
    major_versions: dict[str, str],
    factory_packages: set[str],
) -> list[str]:
    """Assert GNOME required major versions and factory/Hummingbird release identity.

    Which GNOME packages must carry `.bfin` is decided by `[factory]` in
    packages/utah.toml, so moving a package to or from the factory is a manifest
    edit, not a code edit. A factory package must be a factory rebuild; a
    non-factory package comes from Hummingbird but a factory rebuild is also
    approved; a bare Fedora build is never approved.
    """
    errors: list[str] = []
    for pkg in gnome_packages:
        if pkg not in installed:
            continue
        for info in installs_of(installed[pkg]):
            ver, rel = info["version"], info["release"]
            expected_major = major_versions.get(pkg)
            if expected_major:
                match = re.match(r"^(\d+)", ver)
                if not match or match.group(1) != str(expected_major):
                    errors.append(
                        f"GNOME package '{pkg}' version '{ver}' does not match required "
                        f"major version '{expected_major}'"
                    )
            if pkg in factory_packages:
                if ".bfin" not in rel:
                    errors.append(
                        f"GNOME package '{pkg}' release '{rel}' lacks expected factory "
                        "release identity (.bfin)"
                    )
            elif ".bfin" not in rel and ".hum" not in rel:
                # One defect, one error: a bare Fedora release is named as such,
                # anything else unidentified gets the general message.
                if ".fc" in rel:
                    errors.append(
                        f"GNOME package '{pkg}' resolved from unapproved Fedora release '{rel}'"
                    )
                else:
                    errors.append(
                        f"GNOME package '{pkg}' resolved from unapproved release '{rel}' "
                        "(expected Hummingbird `.hum` or factory `.bfin`)"
                    )
    return errors


def verify_parity_origin(
    parity_packages: list[str],
    installed: dict[str, dict[str, Any]],
    factory_parity: set[str] | None = None,
) -> list[str]:
    """Assert parity packages cannot silently resolve from another repository.

    Most parity packages are inherited from Hummingbird's repository. A `.fc`
    release without the `.hum` Hummingbird tag means DNF pulled it from the base
    image's Fedora repository instead of the pinned Hummingbird one.

    `[factory] parity` in packages/utah.toml names the parity packages the
    factory itself supplies. Those are held to the stronger rule: the factory
    publishes them as `.hum<N>.bfin`, which outranks Hummingbird's `.hum<N>`, so
    a copy without `.bfin` means the transaction resolved somewhere other than
    the factory repository -- the silent substitution issue #21 forbids.
    """
    factory_parity = factory_parity or set()
    errors: list[str] = []
    for pkg in parity_packages:
        if pkg not in installed:
            continue
        for info in installs_of(installed[pkg]):
            rel = info["release"]
            if pkg in factory_parity:
                if ".bfin" not in rel:
                    errors.append(
                        f"Parity package '{pkg}' is supplied by the factory but resolved "
                        f"from release '{rel}', which lacks the factory identity (.bfin)"
                    )
                continue
            if ".fc" in rel and ".hum" not in rel and ".bfin" not in rel:
                errors.append(
                    f"Parity package '{pkg}' resolved from unapproved Fedora release '{rel}'"
                )
    return errors


def normalize_baseurl(url: str) -> str:
    """Normalize a baseurl so two spellings of the same URL compare equal."""
    value = url.strip().rstrip("/")
    if not value:
        return ""
    value = re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", r"$\1", value)
    scheme, sep, rest = value.partition("://")
    if not sep:
        return value.lower()
    host, slash, path = rest.partition("/")
    return f"{scheme.lower()}://{host.lower()}{slash}{path}"


def split_baseurls(raw: str) -> list[str]:
    """Split a baseurl option into the origins DNF would fetch from."""
    return [entry for entry in re.split(r"[\s,]+", raw.strip()) if entry]


def repo_pin_errors(
    section_name: str,
    parser: configparser.ConfigParser,
    source: str,
    expected_baseurls: dict[str, tuple[str, ...]],
) -> list[str]:
    """Check that an allowlisted repository serves the baseurl it is pinned to."""
    declared = expected_baseurls.get(section_name)
    if not declared:
        return [
            f"Allowlisted repository '{section_name}' is enabled in {source} but has "
            "no pinned baseurl in [repositories.baseurls]; an id on the allowlist is "
            "not approval of an unknown origin"
        ]
    baseurl = parser.get(section_name, "baseurl", fallback="").strip()
    indirection = next(
        (key for key in ("metalink", "mirrorlist")
         if parser.get(section_name, key, fallback="").strip()),
        "",
    )
    if indirection:
        return [
            f"Allowlisted repository '{section_name}' is enabled in {source} and "
            f"resolves via {indirection}; DNF merges those mirrors with any baseurl the "
            f"section declares, so only a pinned baseurl is approved "
            f"(expected one of: {', '.join(sorted(declared))})"
        ]
    if not baseurl:
        return [
            f"Allowlisted repository '{section_name}' is enabled in {source} and declares "
            "no baseurl; only a pinned baseurl is approved (expected one of: "
            f"{', '.join(sorted(declared))})"
        ]
    pinned = {normalize_baseurl(url) for url in declared}
    unpinned = [url for url in split_baseurls(baseurl) if normalize_baseurl(url) not in pinned]
    if unpinned:
        listed = ", ".join(f"'{url}'" for url in unpinned)
        return [
            f"Repository '{section_name}' is enabled in {source} with unpinned baseurl "
            f"{listed}; expected one of: {', '.join(sorted(declared))}"
        ]
    return []


def repo_security_option_errors(
    section_name: str,
    parser: configparser.ConfigParser,
    source: str,
) -> list[str]:
    """Name options that reroute or weaken an allowlisted repository's fetch."""
    errors: list[str] = []
    proxy = parser.get(section_name, "proxy", fallback="").strip()
    if proxy:
        errors.append(
            f"Allowlisted repository '{section_name}' is enabled in {source} with "
            f"proxy={proxy}; a proxy routes fetches through an origin the allowlist "
            "does not name"
        )
    sslverify = parser.get(section_name, "sslverify", fallback="").strip()
    if sslverify.lower() in DISABLED_VALUES:
        errors.append(
            f"Allowlisted repository '{section_name}' is enabled in {source} with "
            f"sslverify={sslverify}; disabling TLS verification accepts any certificate "
            "the origin presents"
        )
    return errors


def check_repo_sections(
    parser: configparser.ConfigParser,
    source: str,
    allowed_repos: set[str],
    *,
    expected_baseurls: dict[str, tuple[str, ...]] | None,
) -> list[str]:
    """Apply the allowlist to every section of an already-parsed config."""
    errors: list[str] = []
    for section_name in parser.sections():
        if not is_repo_enabled(parser.get(section_name, "enabled", fallback="1")):
            if section_name in allowed_repos:
                errors.extend(repo_security_option_errors(section_name, parser, source))
                if expected_baseurls is not None:
                    errors.extend(repo_pin_errors(section_name, parser, source, expected_baseurls))
            continue
        if section_name in allowed_repos:
            errors.extend(repo_security_option_errors(section_name, parser, source))
        baseurl = parser.get(section_name, "baseurl", fallback="").lower()
        is_fedora = "fedora" in section_name.lower() or "fedora" in baseurl
        if is_fedora:
            errors.append(
                f"Fedora repository '{section_name}' is enabled in {source}; Fedora "
                "repositories are forbidden at runtime"
            )
        elif section_name not in allowed_repos:
            errors.append(
                f"Unapproved repository '{section_name}' is enabled in {source}; "
                f"allowed repositories: {sorted(allowed_repos)}"
            )
        elif expected_baseurls is not None:
            errors.extend(repo_pin_errors(section_name, parser, source, expected_baseurls))
    return errors


def builder_only_repo_files(repos_dir: Path) -> set[Path]:
    """Only skip marked files copied into builders, never into the final stage."""
    root = repos_dir.parent
    containerfile = root / "Containerfile"
    if not containerfile.is_file():
        return set()
    stages: list[set[Path]] = []
    text = containerfile.read_text(encoding="utf-8").replace("\\\n", " ")
    for line in text.splitlines():
        words = shlex.split(line, comments=True)
        if not words:
            continue
        if words[0].upper() == "FROM":
            stages.append(set())
        elif words[0].upper() == "COPY" and stages:
            # Cross-stage sources are not checkout paths and cannot authorize a skip.
            if any(word.startswith("--from=") for word in words):
                continue
            sources = [word for word in words[1:-1] if not word.startswith("--")]
            for source in sources:
                for path in root.glob(source):
                    if path.is_dir():
                        stages[-1].update(p.resolve() for p in path.rglob("*.repo"))
                    elif path.suffix == ".repo":
                        stages[-1].add(path.resolve())
    if not stages:
        return set()
    return set().union(*stages[:-1]) - stages[-1]


def verify_repository_policy(
    repos_dir: Path,
    allowed_repos: set[str],
    *,
    expected_baseurls: dict[str, tuple[str, ...]] | None,
    check_mode: bool = False,
) -> list[str]:
    """Prove the system exposes only explicitly allowed runtime RPM repositories.

    In check_mode, a marked builder-only file is skipped only when Containerfile
    copies it into a builder and not into the final runtime stage. A comment
    alone cannot exempt a runtime repository from policy.
    """
    errors: list[str] = []
    if not repos_dir.is_dir():
        return errors
    builder_files = builder_only_repo_files(repos_dir) if check_mode else set()
    for repo_file in sorted(repos_dir.glob("*.repo")):
        try:
            file_text = repo_file.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            errors.append(f"Could not read repo file {repo_file}: {e}")
            continue
        if check_mode and "# builder-only: true" in file_text:
            if repo_file.resolve() in builder_files:
                continue
            errors.append(f"Builder-only repository {repo_file.name} has no exclusive builder COPY")
        parser = configparser.ConfigParser(interpolation=None)
        try:
            parser.read_string(file_text)
        except Exception as e:  # noqa: BLE001 - report and continue scanning
            errors.append(f"Could not parse repo file {repo_file}: {e}")
            continue
        errors.extend(
            check_repo_sections(
                parser, repo_file.name, allowed_repos,
                expected_baseurls=expected_baseurls,
            )
        )
    return errors


def resolve_build_timestamp(environ: dict[str, str] | None = None) -> tuple[str | None, str]:
    """Return the report's build stamp and where it was read from.

    A build that does not export SOURCE_DATE_EPOCH gets no timestamp at all.
    Stamping a fixed sentinel epoch instead was worse than recording nothing:
    the report asserted a build date (2004-12-28) that was never true, and the
    wall clock would make an otherwise reproducible report differ per build.
    """
    env = os.environ if environ is None else environ
    raw = env.get("SOURCE_DATE_EPOCH")
    if not raw:
        return None, "unset"
    try:
        epoch = int(raw)
        return (
            datetime.datetime.fromtimestamp(epoch, tz=datetime.timezone.utc).isoformat(),
            "SOURCE_DATE_EPOCH",
        )
    except (ValueError, OverflowError, OSError):
        print(
            f"WARNING: unusable SOURCE_DATE_EPOCH={raw!r}; the package-origin report "
            "records no build timestamp",
            file=sys.stderr,
        )
        return None, "unusable"


def read_factory_pin(repo_file: Path = Path(FACTORY_REPO_PATH)) -> str | None:
    """The package factory digest stamped into the pinned repository file.

    `scripts/bump-factory-pin.py` moves this stamp together with the
    Containerfile's `ARG PACKAGE_IMAGE_SHA`, so it names the exact factory image
    the contract's NEVRAs were installed from.
    """
    try:
        text = repo_file.read_text(encoding="utf-8")
    except OSError:
        return None
    match = FACTORY_PIN_RE.search(text)
    return match.group("digest") if match else None


def generate_provenance_report(
    installed: dict[str, dict[str, Any]],
    flavor: str,
    allowed_repos: set[str],
    package_sections: dict[str, str],
    output_dir: Path = Path(DEFAULT_REPORT_DIR),
) -> dict[str, Any]:
    """Generate and retain the resolved package-origin/NEVRA report with build provenance."""
    packages_data: dict[str, dict[str, Any]] = {}
    factory_count = hummingbird_count = other_count = 0
    for name in sorted(installed.keys()):
        info = installed[name]
        origin = info["origin"]
        if origin == "factory":
            factory_count += 1
        elif origin == "hummingbird":
            hummingbird_count += 1
        else:
            other_count += 1
        packages_data[name] = {
            "name": info["name"], "epoch": info["epoch"], "version": info["version"],
            "release": info["release"], "arch": info["arch"], "nevra": info["nevra"],
            "origin": origin, "section": package_sections.get(name, "unknown"),
        }
        copies = installs_of(info)
        if len(copies) > 1:
            packages_data[name]["installs"] = [
                {"nevra": c["nevra"], "release": c["release"],
                 "arch": c["arch"], "origin": c["origin"]} for c in copies
            ]
    timestamp, timestamp_source = resolve_build_timestamp()
    # Which factory and which base the NEVRAs came from is the other half of
    # provenance: without it the report says what is installed but not what it
    # was composed from.
    factory_pin = read_factory_pin() or os.environ.get("PACKAGE_IMAGE_SHA") or None
    base_image = os.environ.get("BASE_IMAGE") or None
    base_image_digest = base_image.split("@", 1)[1] if base_image and "@" in base_image else None
    report: dict[str, Any] = {
        "build_provenance": {
            "flavor": flavor, "image": os.environ.get("IMAGE_NAME", "utah"),
            "version": os.environ.get("VERSION", "testing"),
            "timestamp": timestamp, "timestamp_source": timestamp_source,
            "factory_pin": factory_pin,
            "base_image": base_image, "base_image_digest": base_image_digest,
            "contract_packages": len(installed),
            "factory_packages_count": factory_count,
            "hummingbird_packages_count": hummingbird_count,
            "other_packages_count": other_count,
            "allowed_repositories": sorted(allowed_repos),
        },
        "packages": packages_data,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    json_file = output_dir / "package-origins.json"
    txt_file = output_dir / "package-origins.txt"
    json_file.write_text(json.dumps(report, indent=2) + "\n")
    lines = [
        "# Utah Package Origin and NEVRA Report", f"# Flavor: {flavor}",
        f"# Contract packages: {len(installed)}", f"# Factory rebuilds (.bfin): {factory_count}",
        f"# Hummingbird packages (.hum): {hummingbird_count}", f"# Other: {other_count}",
        f"# Factory pin: {factory_pin or 'unknown'}",
        f"# Base image: {base_image or 'unknown'}",
        f"# Generated: {timestamp or 'unstamped (' + timestamp_source + ')'}", "",
        f"{'NAME':<35} {'NEVRA':<50} {'ORIGIN':<15} {'SECTION':<15}",
        f"{'-'*35} {'-'*50} {'-'*15} {'-'*15}",
    ]
    for name, data in packages_data.items():
        for copy in data.get("installs") or [data]:
            lines.append(
                f"{data['name']:<35} {copy['nevra']:<50} "
                f"{copy['origin']:<15} {data['section']:<15}"
            )
    txt_file.write_text("\n".join(lines) + "\n")
    return report


def _parse_sections(overlay: Path, resolved: list[str] | None, manifest: Path) -> tuple[list, list, list, list, list]:
    """Split the contract into bluefin/gnome/parity/hardware/services buckets.

    When `resolved` is given (an image build), the install set is the source of
    truth -- asserting it avoids the drift where install added something this
    check never recomputed. Otherwise the manifests are the source: the
    bluefin parity packages live in the manifest's [fedora] section, and the
    desktop/parity/service packages are read straight from the overlay sections.
    """
    if resolved is not None:
        gnome_names = set(section(overlay, "gnome"))
        parity_names = set(section(overlay, "parity"))
        hardware_names = set(section(overlay, "hardware"))
        service_names = set(section(overlay, "services"))
        overlay_names = gnome_names | parity_names | hardware_names | service_names
        return (
            [p for p in resolved if p not in overlay_names],
            [p for p in resolved if p in gnome_names],
            [p for p in resolved if p in parity_names],
            [p for p in resolved if p in hardware_names],
            [p for p in resolved if p in service_names],
        )
    unavailable = set(section(overlay, "unavailable"))
    return (
        [p for p in section(manifest, "fedora") if p not in unavailable],
        section(overlay, "gnome"),
        section(overlay, "parity"),
        section(overlay, "hardware"),
        section(overlay, "services"),
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument(
        "--no-report", action="store_true",
        help="verify only; do not write the retained provenance report.",
    )
    parser.add_argument("manifest", type=Path)
    parser.add_argument("overlay", type=Path, nargs="?", default=None)
    args = parser.parse_args()
    overlay = args.overlay or args.manifest.with_name("utah.toml")

    if not overlay.exists():
        print(f"ERROR: Overlay manifest '{overlay}' does not exist", file=sys.stderr)
        return 1

    flavor = os.environ.get("IMAGE_FLAVOR", "main")
    # Prefer the set install-packages.py actually resolved. Recomputing it here
    # is what let the two drift once: install added [fedora_v<major>] for the
    # running release and this check never did, so a contract package was
    # installed but never verified. The manifest path is the off-image fallback.
    resolved = Path(CONTRACT_PATH)
    if resolved.exists():
        contract = [line for line in resolved.read_text().split() if line]
        bluefin, gnome, parity, hardware, services = _parse_sections(overlay, contract, args.manifest)
    else:
        bluefin, gnome, parity, hardware, services = _parse_sections(overlay, None, args.manifest)
    nvidia = list(NVIDIA_PACKAGES) if "nvidia" in flavor else []
    expected = [*bluefin, *gnome, *parity, *hardware, *services, *nvidia]

    overlay_data = tomllib.loads(overlay.read_text())
    try:
        major_versions = overlay_data["gnome"]["versions"]
    except KeyError:
        print(f"ERROR: Overlay manifest '{overlay}' is missing [gnome.versions] section",
              file=sys.stderr)
        return 1
    try:
        allowed_repos = set(overlay_data["repositories"]["allowed"])
    except KeyError:
        print(f"ERROR: Overlay manifest '{overlay}' is missing [repositories.allowed] section",
              file=sys.stderr)
        return 1
    try:
        repo_baseurls = {
            repo_id: tuple(urls)
            for repo_id, urls in overlay_data["repositories"]["baseurls"].items()
        }
    except KeyError:
        print(f"ERROR: Overlay manifest '{overlay}' is missing [repositories.baseurls] section",
              file=sys.stderr)
        return 1
    unpinned = sorted(allowed_repos - repo_baseurls.keys())
    if unpinned:
        print(
            f"ERROR: Overlay manifest '{overlay}' allows repositories with no pinned "
            f"baseurl in [repositories.baseurls]: {', '.join(unpinned)}",
            file=sys.stderr,
        )
        return 1
    factory_packages = set(section(overlay, "factory"))
    factory_parity = set(section(overlay, "factory", "parity"))
    # [factory].packages is the GNOME identity contract; other sections use
    # explicit factory buckets (currently parity), never an inert declaration.
    for pkg in factory_packages:
        assert pkg in set(section(overlay, "gnome")), (
            f"Factory package '{pkg}' is not declared in the [gnome] section"
        )
    for pkg in factory_parity:
        assert pkg in set(section(overlay, "parity")), (
            f"Factory parity package '{pkg}' is not declared in the [parity] section"
        )

    package_sections: dict[str, str] = {}
    for p in bluefin:
        package_sections[p] = "bluefin"
    for p in gnome:
        package_sections[p] = "gnome"
    for p in parity:
        package_sections[p] = "parity"
    for p in hardware:
        package_sections[p] = "hardware"
    for p in services:
        package_sections[p] = "services"
    for p in nvidia:
        package_sections[p] = "nvidia"

    print(
        f"Verifying {len(bluefin)} Bluefin packages, {len(gnome)} GNOME desktop packages,"
        f" {len(parity)} parity packages, {len(hardware)} firmware packages,"
        f" {len(services)} desktop service packages, and {len(nvidia)} NVIDIA packages",
        flush=True,
    )
    if args.check:
        assert len(set(expected)) == len(expected), "RPM contract contains duplicate package names"
        for pkg in factory_packages:
            assert pkg in gnome, f"Factory package '{pkg}' is not declared in the [gnome] section"
        for pkg in factory_parity:
            assert pkg in parity, (
                f"Factory parity package '{pkg}' is not declared in the [parity] section"
            )
        # A [gnome.versions] key that names no [gnome] package asserts nothing:
        # verify_gnome_contract looks versions up by package name, so a typo
        # would silently drop that package's major-version claim on-image.
        gnome_names = set(gnome)
        for pkg in major_versions:
            assert pkg in gnome_names, (
                f"[gnome.versions] key '{pkg}' is not declared in the [gnome] section"
            )
        repo_errors = verify_repository_policy(
            args.manifest.parent, allowed_repos,
            expected_baseurls=repo_baseurls, check_mode=True,
        )
        if repo_errors:
            for err in repo_errors:
                print(f"ERROR: {err}", file=sys.stderr)
            return 1
        print(
            "RPM contract valid; repository policy holds for the runtime .repo files in "
            f"{args.manifest.parent}."
        )
        return 0

    missing = [pkg for pkg in expected if not is_installed(pkg)]
    if missing:
        print(f"ERROR: {len(missing)} of {len(expected)} contract packages are not installed:",
              file=sys.stderr)
        for pkg in missing:
            print(f"  - {pkg}", file=sys.stderr)
        return 1
    print(f"All {len(expected)} contract packages are present.")

    installed, missing_nevra = query_packages(expected)
    if missing_nevra:
        print(
            f"ERROR: {len(missing_nevra)} of {len(expected)} contract packages could not be "
            "queried via RPM:",
            file=sys.stderr,
        )
        for pkg in missing_nevra:
            print(f"  - {pkg}", file=sys.stderr)
        return 1

    attestation_errors: list[str] = []
    attestation_errors.extend(
        verify_gnome_contract(gnome, installed, major_versions, factory_packages)
    )
    attestation_errors.extend(verify_parity_origin(parity, installed, factory_parity))

    if attestation_errors:
        print(
            f"ERROR: {len(attestation_errors)} supply-chain / repository contract violation(s):",
            file=sys.stderr,
        )
        for err in attestation_errors:
            print(f"  - {err}", file=sys.stderr)
        return 1

    # Attest the composed image's runtime RPM repositories against the same
    # allowlist --check already applies to packages/ (#454, #513). The
    # Hummingbird base image ships its own repo files; without this scan they
    # pass into the runtime unattested -- the issue's "fedora or any unapproved
    # enabled RPM repository" claim covers them too. check_mode=False because the
    # runtime image never carries a builder-only repo file; the v4l2loopback
    # stage's fedora-44.repo is never copied into this layer (Containerfile, v4l2
    # stage). Scan every dnf5 default reposdir (#513): a repo file the base ships
    # in /etc/distro.repos.d or /usr/share/dnf5/repos.d is enabled at runtime
    # just as one in /etc/yum.repos.d, so scanning only the first would leave it
    # invisible to the allowlist. Also scan dnf5's two repo-override drop-in dirs
    # (#524): a .repo file there can flip enabled=/baseurl= on a repo id the gate
    # saw disabled, so an override that re-enables one is gated the same way.
    repo_errors: list[str] = []
    for repos_dir in RUNTIME_REPOS_DIRS:
        repo_errors.extend(
            verify_repository_policy(
                repos_dir, allowed_repos,
                expected_baseurls=repo_baseurls, check_mode=False,
            )
        )
    if repo_errors:
        for err in repo_errors:
            print(f"ERROR: {err}", file=sys.stderr)
        return 1

    report_dir = Path(os.environ.get("UTAH_REPORT_DIR", DEFAULT_REPORT_DIR))
    report = None
    if not args.no_report:
        try:
            report = generate_provenance_report(
                installed, flavor, allowed_repos, package_sections, report_dir
            )
        except OSError as err:
            print(
                f"ERROR: could not retain provenance report in {report_dir}: {err}",
                file=sys.stderr,
            )
            return 1
    print(
        f"All {len(expected)} contract packages verified (GNOME versions, factory rebuilds, "
        "parity origin)."
    )
    if report is None:
        print("Skipped provenance report retention (--no-report).")
    else:
        print(
            f"Retained provenance report for {len(installed)} packages "
            f"({report['build_provenance']['factory_packages_count']} factory, "
            f"{report['build_provenance']['hummingbird_packages_count']} hummingbird) "
            f"in {report_dir / 'package-origins.json'}."
        )

    if "nvidia" not in flavor:
        return 0

    # Assert what a source build actually produces: a module for every kernel
    # the image can boot, and the userspace that goes with it.
    ogc = Path("/usr/lib/utah/ogc-kernel-release")
    ogc_release = ogc.read_text().strip() if ogc.exists() else None

    # This deliberately mirrors install-nvidia.sh, including its fallback. `rpm
    # -q kernel` is not reliable here: `kernel` is a metapackage a bootc base may
    # not carry, and on failure rpm prints "package kernel is not installed" to
    # stdout -- whose last word is "installed", which this used to accept as a
    # release string and then report a missing module for a kernel of that name.
    base = subprocess.run(["rpm", "-q", "kernel", "--qf", "%{VERSION}-%{RELEASE}.%{ARCH}\n"],
                          capture_output=True, text=True).stdout.split()
    base = base[-1].strip() if base else ""
    # Identify the kernel by its module tree, not by a build tree. A build tree
    # only exists while kernel-devel is installed, and install-nvidia.sh removes
    # that again once the module is compiled -- 215 MiB there is no reason to
    # ship. Requiring one here meant plain nvidia could never pass: the OGC
    # flavors only satisfied it because install-ogc-kernel.sh leaves its own
    # tree behind. A module tree is what says the image can boot that kernel,
    # which is the thing being asserted.
    if not base or not Path(f"/usr/lib/modules/{base}").is_dir():
        candidates = sorted(d.name for d in Path("/usr/lib/modules").glob("*")
                            if d.name != ogc_release and d.is_dir())
        if not candidates:
            print("ERROR: no kernel module tree found; cannot verify NVIDIA modules",
                  file=sys.stderr)
            return 1
        base = candidates[-1]

    releases = [base]
    if flavor == "nvidia-gaming":
        releases.append(ogc.read_text().strip())

    failed = False
    for release in releases:
        release = release.strip() if release else ""
        if not release:
            print("ERROR: empty kernel release; no kernel to check NVIDIA module against",
                  file=sys.stderr)
            failed = True
            continue
        module = Path(f"/usr/lib/modules/{release}/extra/nvidia/nvidia.ko")
        if not module.exists():
            print(f"ERROR: NVIDIA module missing for kernel {release}", file=sys.stderr)
            failed = True
    for path in (Path("/usr/bin/nvidia-smi"), Path("/usr/lib/utah/nvidia-driver-version")):
        if not path.exists():
            print(f"ERROR: NVIDIA userspace incomplete, {path} is missing", file=sys.stderr)
            failed = True
    if failed:
        return 1
    version = Path("/usr/lib/utah/nvidia-driver-version").read_text().strip()
    print(f"NVIDIA {version} present for: {', '.join(releases)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
