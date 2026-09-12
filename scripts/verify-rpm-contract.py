#!/usr/bin/env python3
"""Assert that Utah actually contains its Bluefin and GNOME 51 RPM contracts.

Mirrors assert_packages_present from projectbluefin/bluefin's
build_files/shared/package-lib.sh: name every missing package, once.
In addition, validates GNOME 51 major versions, verifies factory rebuild release
identity (.hum1.bfin) preventing silent fallbacks to base repositories, enforces
the final runtime repository allowlist, and records a resolved package-origin/NEVRA
report with build provenance under /usr/share/utah/package-provenance.json.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
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

# Approved runtime repository IDs explicitly permitted in composed Utah images.
# Fedora repositories or unapproved third-party repositories must fail verification.
DEFAULT_ALLOWED_REPOS: tuple[str, ...] = (
    "utah-packages",
    "public-hummingbird-x86_64-rpms",
    "nvidia-container-toolkit",
)

# Release-identity patterns for the factory (.hum1.bfin) and Hummingbird
# (.hum) rebuild conventions. Overridable via [supply_chain] in utah.toml so a
# dist-tag convention change on either side is a data edit, not a code change.
DEFAULT_FACTORY_RELEASE_PATTERN = r"\.bfin"
DEFAULT_HUMMINGBIRD_RELEASE_PATTERN = r"\.hum\d*(\.bfin)?"

# Expected major versions for GNOME contract packages.
DEFAULT_GNOME_MAJORS: dict[str, str] = {
    "gnome-control-center": "51",
    "gnome-session": "51",
    "gnome-settings-daemon": "51",
    "gnome-shell": "51",
    "gsettings-desktop-schemas": "51",
    "mutter": "51",
    "xdg-desktop-portal-gnome": "51",
    "gtk4": "4",
    "libadwaita": "1",
    "xdg-desktop-portal": "1",
}


def section(path: Path, name: str) -> list[str]:
    data = tomllib.loads(path.read_text())
    return list(data.get(name, {}).get("packages", []))


def is_installed(pkg: str) -> bool:
    return subprocess.run(
        ["rpm", "-q", pkg], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    ).returncode == 0


def query_package_nevras(packages: list[str]) -> dict[str, dict[str, str]]:
    """Query NEVRAs and provenance attributes for installed packages in batch."""
    if not packages:
        return {}
    qf = "%{NAME}|%|EPOCH?{%{EPOCH}}:{0}||%{VERSION}|%{RELEASE}|%{ARCH}|%{SOURCERPM}|%|VENDOR?{%{VENDOR}}:{(none)}|\n"
    res = subprocess.run(
        ["rpm", "-q", f"--queryformat={qf}", *packages],
        capture_output=True,
        text=True,
        check=False,
    )
    pkg_map: dict[str, dict[str, str]] = {}
    for line in res.stdout.splitlines():
        line = line.strip()
        if not line or "|" not in line:
            continue
        parts = line.split("|")
        if len(parts) >= 7:
            name, epoch, version, release, arch, sourcerpm, vendor = parts[:7]
            pkg_map[name] = {
                "name": name,
                "epoch": epoch,
                "version": version,
                "release": release,
                "arch": arch,
                "sourcerpm": sourcerpm,
                "vendor": vendor,
                "nevra": f"{name}-{epoch}:{version}-{release}.{arch}",
            }
    return pkg_map


def verify_gnome_packages(
    gnome_pkgs: list[str],
    pkg_map: dict[str, dict[str, str]],
    expected_majors: dict[str, str],
    hummingbird_pattern: str = DEFAULT_HUMMINGBIRD_RELEASE_PATTERN,
) -> list[str]:
    """Verify GNOME contract packages for required major version and release identity."""
    errors: list[str] = []
    for pkg in gnome_pkgs:
        info = pkg_map.get(pkg)
        if not info:
            continue
        ver = info["version"]
        rel = info["release"]
        exp_maj = expected_majors.get(pkg)
        if exp_maj:
            m = re.match(r"^(\d+)", ver)
            actual_maj = m.group(1) if m else ""
            if actual_maj != str(exp_maj):
                errors.append(
                    f"GNOME package '{pkg}' version '{ver}' does not match required major version '{exp_maj}'"
                )
        # Release identity must match factory (.hum1.bfin) or Hummingbird (.hum)
        if not re.search(hummingbird_pattern, rel):
            errors.append(
                f"GNOME package '{pkg}' release '{rel}' does not match expected factory/Hummingbird release identity"
            )
        if ".fc" in rel:
            errors.append(
                f"GNOME package '{pkg}' release '{rel}' carries unapproved Fedora release identity"
            )
    return errors


def verify_factory_packages(
    factory_pkgs: list[str],
    pkg_map: dict[str, dict[str, str]],
    factory_pattern: str = DEFAULT_FACTORY_RELEASE_PATTERN,
) -> list[str]:
    """Ensure Bluefin parity packages expected from factory carry .bfin and did not silently resolve from base repos."""
    errors: list[str] = []
    for pkg in factory_pkgs:
        info = pkg_map.get(pkg)
        if not info:
            continue
        rel = info["release"]
        if not re.search(factory_pattern, rel):
            errors.append(
                f"Bluefin parity package '{pkg}' expected from factory (.bfin), but resolved with release '{rel}' (silent repository fallback)"
            )
        if ".fc" in rel:
            errors.append(
                f"Bluefin parity package '{pkg}' carries unapproved Fedora release identity '{rel}'"
            )
    return errors


def verify_repository_allowlist(
    allowlist: set[str],
    repos_dir: Path = Path("/etc/yum.repos.d"),
) -> tuple[list[str], list[str]]:
    """Enforce repository allowlist on /etc/yum.repos.d and package manager status."""
    errors: list[str] = []
    enabled_repos: set[str] = set()

    if repos_dir.is_dir():
        for repo_file in sorted(repos_dir.glob("*.repo")):
            try:
                content = repo_file.read_text()
            except OSError:
                continue
            current_section: str | None = None
            section_enabled = True
            is_fedora = False
            for line in content.splitlines():
                line = line.strip()
                if not line or line.startswith("#") or line.startswith(";"):
                    continue
                sec_match = re.match(r"^\[([^\]]+)\]", line)
                if sec_match:
                    if current_section is not None and section_enabled:
                        enabled_repos.add(current_section)
                        if is_fedora:
                            errors.append(
                                f"Repository allowlist failure: Fedora repository '{current_section}' is enabled in {repo_file.name}"
                            )
                        elif current_section not in allowlist:
                            errors.append(
                                f"Repository allowlist failure: unapproved repository '{current_section}' is enabled in {repo_file.name} (allowlist: {sorted(allowlist)})"
                            )
                    current_section = sec_match.group(1).strip()
                    section_enabled = True
                    is_fedora = "fedora" in current_section.lower() or "rawhide" in current_section.lower()
                elif "=" in line:
                    k, v = line.split("=", 1)
                    k = k.strip().lower()
                    v = v.strip().lower()
                    if k == "enabled":
                        section_enabled = v in ("1", "true", "yes")
                    elif k in ("baseurl", "metalink", "mirrorlist"):
                        if "fedoraproject.org" in v or "fedora" in v:
                            is_fedora = True

            if current_section is not None and section_enabled:
                enabled_repos.add(current_section)
                if is_fedora:
                    errors.append(
                        f"Repository allowlist failure: Fedora repository '{current_section}' is enabled in {repo_file.name}"
                    )
                elif current_section not in allowlist:
                    errors.append(
                        f"Repository allowlist failure: unapproved repository '{current_section}' is enabled in {repo_file.name} (allowlist: {sorted(allowlist)})"
                    )

    dnf_bin = shutil.which("dnf5") or shutil.which("dnf")
    if dnf_bin:
        res = subprocess.run(
            [dnf_bin, "repolist", "--enabled"],
            capture_output=True,
            text=True,
            check=False,
        )
        if res.returncode == 0:
            for line in res.stdout.splitlines():
                line = line.strip()
                if not line or line.startswith("repo id") or line.startswith("Loaded plugins") or line.startswith("Updating and loading"):
                    continue
                parts = line.split()
                if parts:
                    repo_id = parts[0]
                    enabled_repos.add(repo_id)
                    if "fedora" in repo_id.lower() or "rawhide" in repo_id.lower():
                        msg = f"Repository allowlist failure: Fedora repository '{repo_id}' reported enabled by package manager"
                        if msg not in errors:
                            errors.append(msg)
                    elif repo_id not in allowlist:
                        msg = f"Repository allowlist failure: unapproved repository '{repo_id}' reported enabled by package manager (allowlist: {sorted(allowlist)})"
                        if msg not in errors:
                            errors.append(msg)

    return errors, sorted(enabled_repos)


def retain_provenance_report(
    pkg_map: dict[str, dict[str, str]],
    expected_categories: dict[str, str],
    factory_pkgs: set[str],
    gnome_majors: dict[str, str],
    enabled_repos: list[str],
    allowlist: set[str],
    flavor: str,
    output_path: Path = Path("/usr/share/utah/package-provenance.json"),
) -> None:
    """Retain the resolved package-origin/NEVRA report with build provenance."""
    packages_list = []
    summary_counts = {
        "total_contract_packages": len(pkg_map),
        "factory_packages": 0,
        "hummingbird_packages": 0,
        "nvidia_packages": 0,
        "other_packages": 0,
    }

    for name, info in sorted(pkg_map.items()):
        rel = info["release"]
        vendor = info.get("vendor", "")
        if ".bfin" in rel:
            origin = "factory"
            summary_counts["factory_packages"] += 1
        elif ".hum" in rel:
            origin = "hummingbird"
            summary_counts["hummingbird_packages"] += 1
        elif "nvidia" in name.lower() or "nvidia" in vendor.lower() or "nvidia" in rel.lower():
            origin = "nvidia"
            summary_counts["nvidia_packages"] += 1
        elif ".fc" in rel:
            origin = "fedora"
            summary_counts["other_packages"] += 1
        else:
            origin = "other"
            summary_counts["other_packages"] += 1

        sec = expected_categories.get(name, "contract")
        entry: dict[str, Any] = {
            "name": name,
            "epoch": info["epoch"],
            "version": info["version"],
            "release": info["release"],
            "arch": info["arch"],
            "nevra": info["nevra"],
            "sourcerpm": info["sourcerpm"],
            "vendor": info["vendor"],
            "contract_section": sec,
            "origin": origin,
            "expected_factory": name in factory_pkgs,
        }
        if name in gnome_majors:
            entry["expected_gnome_major"] = gnome_majors[name]
        packages_list.append(entry)

    report = {
        "report_version": "1.0",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "image_flavor": flavor,
        "summary": summary_counts,
        "repositories": {
            "allowlist": sorted(allowlist),
            "enabled": enabled_repos,
            "allowlist_verified": True,
        },
        "packages": packages_list,
    }

    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        report_json = json.dumps(report, indent=2) + "\n"
        output_path.write_text(report_json)
        print(
            f"Retained resolved package-origin/NEVRA report at {output_path} "
            f"({len(packages_list)} packages: {summary_counts['factory_packages']} factory rebuilds, "
            f"{summary_counts['hummingbird_packages']} Hummingbird)",
            flush=True,
        )
    except OSError as exc:
        print(f"WARNING: Could not write package provenance report to {output_path}: {exc}", file=sys.stderr)



def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("manifest", type=Path)
    parser.add_argument("overlay", type=Path, nargs="?", default=None)
    args = parser.parse_args()
    overlay = args.overlay or args.manifest.with_name("utah.toml")

    flavor = os.environ.get("IMAGE_FLAVOR", "main")
    overlay_data = tomllib.loads(overlay.read_text()) if overlay.exists() else {}
    unavailable = set(overlay_data.get("unavailable", {}).get("packages", []))

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
        gnome_names = set(overlay_data.get("gnome", {}).get("packages", []))
        service_names = set(overlay_data.get("services", {}).get("packages", []))
        bluefin = [p for p in contract if p not in gnome_names and p not in service_names]
        gnome = [p for p in contract if p in gnome_names]
        services = [p for p in contract if p in service_names]
    else:
        manifest_data = tomllib.loads(args.manifest.read_text()) if args.manifest.exists() else {}
        bluefin = [p for p in manifest_data.get("fedora", {}).get("packages", []) if p not in unavailable]
        gnome = list(overlay_data.get("gnome", {}).get("packages", []))
        services = list(overlay_data.get("services", {}).get("packages", []))

    nvidia = list(NVIDIA_PACKAGES) if "nvidia" in flavor else []
    expected = [*bluefin, *gnome, *services, *nvidia]

    factory_pkgs = list(overlay_data.get("factory", {}).get("packages", []))
    configured_majors = dict(overlay_data.get("gnome", {}).get("major_versions", {}))
    expected_gnome_majors = {**DEFAULT_GNOME_MAJORS, **configured_majors}

    raw_allowlist = overlay_data.get("repositories", {}).get("allowlist", DEFAULT_ALLOWED_REPOS)
    repo_allowlist = set(raw_allowlist)

    supply_chain_cfg = overlay_data.get("supply_chain", {})
    factory_release_pattern = supply_chain_cfg.get(
        "factory_release_pattern", DEFAULT_FACTORY_RELEASE_PATTERN
    )
    hummingbird_release_pattern = supply_chain_cfg.get(
        "hummingbird_release_pattern", DEFAULT_HUMMINGBIRD_RELEASE_PATTERN
    )

    print(
        f"Verifying {len(bluefin)} Bluefin packages, {len(gnome)} GNOME desktop packages,"
        f" {len(services)} desktop service packages, and {len(nvidia)} NVIDIA packages",
        flush=True,
    )
    if args.check:
        assert len(set(expected)) == len(expected), "RPM contract contains duplicate package names"
        if factory_pkgs:
            assert len(set(factory_pkgs)) == len(factory_pkgs), "Factory package list contains duplicates"
            overlap = set(factory_pkgs) & unavailable
            assert not overlap, f"Factory packages overlap with unavailable: {overlap}"
        assert not any("fedora" in r.lower() for r in repo_allowlist), "Fedora repository in allowlist"
        assert "utah-packages" in repo_allowlist, "utah-packages missing from repository allowlist"
        assert "public-hummingbird-x86_64-rpms" in repo_allowlist, "public-hummingbird missing from repository allowlist"
        print(
            f"RPM contract syntax and supply-chain policy is valid: "
            f"{len(gnome)} GNOME desktop packages, {len(factory_pkgs)} factory rebuilds, "
            f"repository allowlist: {', '.join(sorted(repo_allowlist))}"
        )
        return 0

    missing = [pkg for pkg in expected if not is_installed(pkg)]
    if missing:
        print(
            f"ERROR: {len(missing)} of {len(expected)} contract packages are not installed:",
            file=sys.stderr,
        )
        for pkg in missing:
            print(f"  - {pkg}", file=sys.stderr)
        return 1
    print(f"All {len(expected)} contract packages are present.")

    # Query installed package NEVRAs and provenance attributes
    pkg_map = query_package_nevras(expected)

    # Acceptance Criterion 1: GNOME contract package version & release identity
    validation_errors: list[str] = []
    gnome_errors = verify_gnome_packages(
        gnome, pkg_map, expected_gnome_majors, hummingbird_release_pattern
    )
    validation_errors.extend(gnome_errors)

    # Acceptance Criterion 2: Bluefin parity packages expected from factory cannot silently resolve elsewhere
    factory_errors = verify_factory_packages(factory_pkgs, pkg_map, factory_release_pattern)
    validation_errors.extend(factory_errors)

    # Acceptance Criterion 3: Final repository allowlist fails on Fedora or unapproved repos
    repo_errors, enabled_repos = verify_repository_allowlist(repo_allowlist)
    validation_errors.extend(repo_errors)

    if validation_errors:
        print(f"ERROR: {len(validation_errors)} supply-chain contract verification failures:", file=sys.stderr)
        for err in validation_errors:
            print(f"  - {err}", file=sys.stderr)
        return 1

    print(
        f"Supply-chain attestation passed: GNOME {len(gnome)} packages verified for version and release identity; "
        f"{len(factory_pkgs)} factory parity packages confirmed; "
        f"repository allowlist confirmed ({len(enabled_repos)} enabled repositories)."
    )

    # Acceptance Criterion 4: Retain resolved package-origin/NEVRA report with build provenance
    category_map: dict[str, str] = {}
    for p in bluefin:
        category_map[p] = "bluefin"
    for p in gnome:
        category_map[p] = "gnome"
    for p in services:
        category_map[p] = "services"
    for p in nvidia:
        category_map[p] = "nvidia"

    report_path_str = overlay_data.get("supply_chain", {}).get(
        "report_path", "/usr/share/utah/package-provenance.json"
    )
    retain_provenance_report(
        pkg_map=pkg_map,
        expected_categories=category_map,
        factory_pkgs=set(factory_pkgs),
        gnome_majors=expected_gnome_majors,
        enabled_repos=enabled_repos,
        allowlist=repo_allowlist,
        flavor=flavor,
        output_path=Path(report_path_str),
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
