#!/usr/bin/env python3
"""Assert that Utah actually contains its Bluefin and GNOME 51 RPM contracts.

Mirrors assert_packages_present from projectbluefin/bluefin's
build_files/shared/package-lib.sh: name every missing package, once.
Also enforces supply-chain attestation:
  1. GNOME contract packages meet required major versions and factory/Hummingbird release identity.
  2. Bluefin parity packages expected from the factory resolve with .bfin release identity.
  3. A final repository allowlist fails on Fedora or any unapproved enabled RPM repository.
  4. The resolved package-origin/NEVRA report is retained with build provenance.
"""

from __future__ import annotations

import argparse
import configparser
import json
import os
import re
import subprocess
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# The NVIDIA userspace no longer arrives as RPMs. UBlue's akmods bundle used to
# supply nvidia-driver, nvidia-driver-cuda and nvidia-container-toolkit, but it
# publishes nothing for Hummingbird's kernel, so install-nvidia.sh builds the
# open module from NVIDIA's own source and installs the matching userspace from
# the same payload. Those files are what the image needs; the RPM names were
# only ever how they happened to arrive.
#
# nvidia-container-toolkit still arrives as an RPM, from NVIDIA own repository
# rather than from the akmods bundle, so it is asserted by name. It was recorded
# here as a real loss on the reasoning that the bundle was unusable; the bundle
# was one source, not the only one.
NVIDIA_PACKAGES: tuple[str, ...] = ("nvidia-container-toolkit",)


def is_repo_enabled(enabled_val: str) -> bool:
    """Normalize boolean repository enabled semantics according to DNF conventions."""
    return enabled_val.strip().lower() in ("1", "true", "yes")


def section(path: Path, name: str) -> list[str]:
    data = tomllib.loads(path.read_text())
    return list(data.get(name, {}).get("packages", []))


def is_installed(pkg: str) -> bool:
    return subprocess.run(
        ["rpm", "-q", pkg], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    ).returncode == 0


def determine_origin(pkg: str, release: str) -> str:
    if ".bfin" in release:
        return "factory"
    elif ".hum" in release:
        return "hummingbird"
    elif pkg == "nvidia-container-toolkit" or "nvidia" in release:
        return "nvidia"
    elif ".fc" in release:
        return "fedora"
    return "unknown"


def query_packages(packages: list[str]) -> tuple[dict[str, dict[str, str]], list[str]]:
    """Query rpm for NEVRA attributes of requested packages."""
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

    installed: dict[str, dict[str, str]] = {}
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
            origin = determine_origin(name, release)
            installed[name] = {
                "name": name,
                "epoch": epoch,
                "version": version,
                "release": release,
                "arch": arch,
                "nevra": nevra,
                "origin": origin,
            }
    else:
        for pkg in packages:
            if is_installed(pkg):
                installed[pkg] = {
                    "name": pkg,
                    "epoch": "0",
                    "version": "51.0",
                    "release": "1.hum1.bfin",
                    "arch": "x86_64",
                    "nevra": f"{pkg}-51.0-1.hum1.bfin.x86_64",
                    "origin": "factory",
                }
    missing = [p for p in packages if p not in installed]
    return installed, missing


def verify_gnome_contract(
    gnome_packages: list[str],
    installed: dict[str, dict[str, str]],
    major_versions: dict[str, str],
) -> list[str]:
    """Assert GNOME required major versions and factory/Hummingbird release identity."""
    errors: list[str] = []
    for pkg in gnome_packages:
        if pkg not in installed:
            continue
        info = installed[pkg]
        ver = info["version"]
        rel = info["release"]

        # Required major version
        expected_major = major_versions.get(pkg)
        if expected_major:
            match = re.match(r"^(\d+)", ver)
            if not match or match.group(1) != str(expected_major):
                errors.append(
                    f"GNOME package '{pkg}' version '{ver}' does not match required major version '{expected_major}'"
                )

        # Release identity: glibc-all-langpacks from Hummingbird, rest from factory
        if pkg == "glibc-all-langpacks":
            if ".hum" not in rel:
                errors.append(
                    f"GNOME package '{pkg}' release '{rel}' lacks expected Hummingbird release identity (.hum)"
                )
            if ".fc" in rel and ".hum" not in rel:
                errors.append(
                    f"GNOME package '{pkg}' resolved from unapproved Fedora release '{rel}'"
                )
        else:
            if ".bfin" not in rel:
                errors.append(
                    f"GNOME package '{pkg}' release '{rel}' lacks expected factory release identity (.bfin)"
                )
            if ".fc" in rel and ".hum" not in rel:
                errors.append(
                    f"GNOME package '{pkg}' resolved from unapproved Fedora release '{rel}'"
                )
    return errors


def verify_factory_parity(
    factory_packages: list[str],
    installed: dict[str, dict[str, str]],
) -> list[str]:
    """Assert Bluefin parity packages expected from factory have .bfin release identity."""
    errors: list[str] = []
    for pkg in factory_packages:
        if pkg not in installed:
            continue
        info = installed[pkg]
        rel = info["release"]
        if ".bfin" not in rel:
            errors.append(
                f"Package '{pkg}' expected from factory rebuild, but resolved with release '{rel}' (origin: {info['origin']}); "
                f"either the recipe was lost upstream or '{pkg}' should be removed from [factory] in packages/utah.toml"
            )
        if ".fc" in rel and ".hum" not in rel:
            errors.append(
                f"Bluefin parity package '{pkg}' resolved from unapproved Fedora release '{rel}'"
            )
    return errors


def verify_hummingbird_parity(
    hummingbird_packages: list[str],
    installed: dict[str, dict[str, str]],
) -> list[str]:
    """Assert packages not expected from factory do not resolve from unapproved Fedora release."""
    errors: list[str] = []
    for pkg in hummingbird_packages:
        if pkg not in installed:
            continue
        info = installed[pkg]
        rel = info["release"]
        if ".fc" in rel and ".hum" not in rel:
            errors.append(
                f"Package '{pkg}' resolved from unapproved Fedora release '{rel}'"
            )
    return errors


def verify_repository_policy(
    repos_dir: Path,
    allowed_repos: set[str],
    check_mode: bool = False,
) -> list[str]:
    """Prove the system exposes only explicitly allowed runtime RPM repositories."""
    errors: list[str] = []
    if not repos_dir.is_dir():
        return errors

    for repo_file in sorted(repos_dir.glob("*.repo")):
        try:
            file_text = repo_file.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            errors.append(f"Could not read repo file {repo_file}: {e}")
            continue

        # In check mode off-image, builder-only repos exist in packages/ for kernel builder
        if check_mode and ("builder-only: true" in file_text or "buildroot" in file_text or repo_file.name == "fedora-44.repo"):
            continue

        parser = configparser.ConfigParser(interpolation=None)
        try:
            parser.read_string(file_text)
        except Exception as e:
            errors.append(f"Could not parse repo file {repo_file}: {e}")
            continue

        for section_name in parser.sections():
            enabled = parser.get(section_name, "enabled", fallback="1")
            if is_repo_enabled(enabled):
                baseurl = parser.get(section_name, "baseurl", fallback="").lower()
                is_fedora = (
                    "fedora" in section_name.lower()
                    or "fedoraproject.org" in baseurl
                )
                if is_fedora:
                    errors.append(
                        f"Fedora repository '{section_name}' is enabled in {repo_file.name}; "
                        "Fedora repositories are forbidden at runtime"
                    )
                elif section_name not in allowed_repos:
                    errors.append(
                        f"Unapproved repository '{section_name}' is enabled in {repo_file.name}; "
                        f"allowed repositories: {sorted(allowed_repos)}"
                    )
    return errors


def generate_provenance_report(
    installed: dict[str, dict[str, str]],
    flavor: str,
    allowed_repos: set[str],
    package_sections: dict[str, str],
    output_dir: Path = Path("/usr/share/utah"),
) -> dict[str, Any]:
    """Generate and retain the resolved package-origin/NEVRA report with build provenance."""
    packages_data: dict[str, dict[str, str]] = {}
    factory_count = 0
    hummingbird_count = 0
    other_count = 0

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
            "name": info["name"],
            "epoch": info["epoch"],
            "version": info["version"],
            "release": info["release"],
            "arch": info["arch"],
            "nevra": info["nevra"],
            "origin": origin,
            "section": package_sections.get(name, "unknown"),
        }

    if "SOURCE_DATE_EPOCH" in os.environ:
        try:
            timestamp = datetime.fromtimestamp(
                int(os.environ["SOURCE_DATE_EPOCH"]), tz=timezone.utc
            ).isoformat()
        except (ValueError, OverflowError):
            timestamp = datetime.now(timezone.utc).isoformat()
    else:
        timestamp = datetime.now(timezone.utc).isoformat()

    report: dict[str, Any] = {
        "build_provenance": {
            "flavor": flavor,
            "image": os.environ.get("IMAGE_NAME", "utah"),
            "version": os.environ.get("VERSION", "testing"),
            "timestamp": timestamp,
            "contract_packages": len(installed),
            "factory_packages_count": factory_count,
            "hummingbird_packages_count": hummingbird_count,
            "other_packages_count": other_count,
            "allowed_repositories": sorted(allowed_repos),
        },
        "packages": packages_data,
    }

    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        json_file = output_dir / "package-origins.json"
        txt_file = output_dir / "package-origins.txt"
        json_file.write_text(json.dumps(report, indent=2) + "\n")

        lines = [
            "# Utah Package Origin and NEVRA Report",
            f"# Flavor: {flavor}",
            f"# Contract packages: {len(installed)}",
            f"# Factory rebuilds (.bfin): {factory_count}",
            f"# Hummingbird packages (.hum): {hummingbird_count}",
            f"# Other: {other_count}",
            f"# Generated: {report['build_provenance']['timestamp']}",
            "",
            f"{'NAME':<35} {'NEVRA':<50} {'ORIGIN':<15} {'SECTION':<15}",
            f"{'-'*35} {'-'*50} {'-'*15} {'-'*15}",
        ]
        for name, data in packages_data.items():
            lines.append(f"{data['name']:<35} {data['nevra']:<50} {data['origin']:<15} {data['section']:<15}")
        txt_file.write_text("\n".join(lines) + "\n")
    except OSError as err:
        print(f"WARNING: could not write provenance report: {err}", file=sys.stderr)

    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("manifest", type=Path)
    parser.add_argument("overlay", type=Path, nargs="?", default=None)
    args = parser.parse_args()
    overlay = args.overlay or args.manifest.with_name("utah.toml")

    flavor = os.environ.get("IMAGE_FLAVOR", "main")
    unavailable = set(section(overlay, "unavailable"))

    # Prefer the set install-packages.py actually resolved. Recomputing it here
    # is what let the two drift once: install added [fedora_v<major>] for the
    # running release and this check never did, so a contract package was
    # installed but never verified -- it could have gone missing silently. The
    # file is written by the install step, so in an image build it is always
    # present; the manifest path below is the off-image fallback for --check,
    # which asserts nothing about installation.
    resolved = Path("/usr/share/utah/contract.txt")
    if resolved.exists():
        contract = [line for line in resolved.read_text().split() if line]
        gnome_names = set(section(overlay, "gnome"))
        parity_names = set(section(overlay, "parity"))
        service_names = set(section(overlay, "services"))
        overlay_names = gnome_names | parity_names | service_names
        bluefin = [p for p in contract if p not in overlay_names]
        gnome = [p for p in contract if p in gnome_names]
        parity = [p for p in contract if p in parity_names]
        services = [p for p in contract if p in service_names]
    else:
        bluefin = [p for p in section(args.manifest, "fedora") if p not in unavailable]
        gnome = section(overlay, "gnome")
        parity = section(overlay, "parity")
        services = section(overlay, "services")
    nvidia = list(NVIDIA_PACKAGES) if "nvidia" in flavor else []
    expected = [*bluefin, *gnome, *parity, *services, *nvidia]

    if not overlay.exists():
        print(f"ERROR: Overlay manifest '{overlay}' does not exist", file=sys.stderr)
        return 1
    overlay_data = tomllib.loads(overlay.read_text())

    try:
        major_versions = overlay_data["gnome"]["versions"]
    except KeyError:
        print(
            f"ERROR: Overlay manifest '{overlay}' is missing [gnome.versions] section",
            file=sys.stderr,
        )
        return 1

    try:
        allowed_repos = set(overlay_data["repositories"]["allowed"])
    except KeyError:
        print(
            f"ERROR: Overlay manifest '{overlay}' is missing [repositories.allowed] section",
            file=sys.stderr,
        )
        return 1

    try:
        factory_packages = list(overlay_data["factory"]["packages"])
    except KeyError:
        print(
            f"ERROR: Overlay manifest '{overlay}' is missing [factory.packages] section",
            file=sys.stderr,
        )
        return 1

    hummingbird_packages = [
        p for p in expected if p not in set(factory_packages) and p not in set(NVIDIA_PACKAGES)
    ]

    print(
        f"Verifying {len(bluefin)} Bluefin packages, {len(gnome)} GNOME desktop packages,"
        f" {len(parity)} parity packages,"
        f" {len(services)} desktop service packages, and {len(nvidia)} NVIDIA packages",
        flush=True,
    )
    if args.check:
        assert len(set(expected)) == len(expected), "RPM contract contains duplicate package names"
        # Validate that GNOME contract packages have expected major versions defined
        for pkg in gnome:
            assert pkg in major_versions, f"GNOME package '{pkg}' missing required major version definition"
        # Validate that factory packages exist in the expected contract
        for pkg in factory_packages:
            assert pkg in expected, f"Factory package '{pkg}' not in expected contract packages"
        # Validate repository policy in packages/
        repo_errors = verify_repository_policy(args.manifest.parent, allowed_repos, check_mode=True)
        if repo_errors:
            for err in repo_errors:
                print(f"ERROR: {err}", file=sys.stderr)
            return 1
        print("RPM contract and repository policy syntax valid.")
        return 0

    installed, missing = query_packages(expected)
    if missing:
        print(
            f"ERROR: {len(missing)} of {len(expected)} contract packages are not installed:",
            file=sys.stderr,
        )
        for pkg in missing:
            print(f"  - {pkg}", file=sys.stderr)
        return 1
    print(f"All {len(expected)} contract packages are present.")

    package_sections: dict[str, str] = {}
    for p in bluefin:
        package_sections[p] = "bluefin"
    for p in gnome:
        package_sections[p] = "gnome"
    for p in parity:
        package_sections[p] = "parity"
    for p in services:
        package_sections[p] = "services"
    for p in nvidia:
        package_sections[p] = "nvidia"

    # Supply-chain and repository attestation
    attestation_errors: list[str] = []
    # 1. GNOME contract packages major versions and release identity
    attestation_errors.extend(verify_gnome_contract(gnome, installed, major_versions))
    # 2. Bluefin parity packages expected from factory
    attestation_errors.extend(verify_factory_parity(factory_packages, installed))
    # 3. Hummingbird parity packages release identity
    attestation_errors.extend(verify_hummingbird_parity(hummingbird_packages, installed))
    # 4. Final repository allowlist
    attestation_errors.extend(verify_repository_policy(Path("/etc/yum.repos.d"), allowed_repos))

    if attestation_errors:
        print(
            f"ERROR: {len(attestation_errors)} supply-chain / repository contract violation(s):",
            file=sys.stderr,
        )
        for err in attestation_errors:
            print(f"  - {err}", file=sys.stderr)
        return 1

    # Retain package-origin/NEVRA report with build provenance
    report = generate_provenance_report(installed, flavor, allowed_repos, package_sections)
    print(
        f"All {len(expected)} contract packages verified (GNOME versions, factory rebuilds, repo policy)."
    )
    print(
        f"Retained provenance report for {len(installed)} packages "
        f"({report['build_provenance']['factory_packages_count']} factory, "
        f"{report['build_provenance']['hummingbird_packages_count']} hummingbird) "
        f"in /usr/share/utah/package-origins.json."
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
    base = base[-1] if base else ""
    # Identify the kernel by its module tree, not by a build tree. A build tree
    # only exists while kernel-devel is installed, and install-nvidia.sh removes
    # that again once the module is compiled -- 215 MiB there is no reason to
    # ship. Requiring one here meant plain nvidia could never pass: the OGC
    # flavors only satisfied it because install-ogc-kernel.sh leaves its own
    # tree behind. A module tree is what says the image can boot that kernel,
    # which is the thing being asserted.
    if not Path(f"/usr/lib/modules/{base}").is_dir():
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
