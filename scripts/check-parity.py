#!/usr/bin/env python3
"""Check that Utah's Bluefin package contract matches Bluefin's effective payload.

Bluefin executes package installations across multiple sources:
1. Base Fedora packages (base.toml [fedora])
2. Version-specific Fedora packages (base.toml [fedora_v*])
3. External transactions in 03-packages.sh (e.g. tailscale from tailscale-stable,
   uupd from ublue-os/packages COPR)
4. Multimedia transactions:
   - Override packages synced from fedora-multimedia (base.toml [multimedia_overrides])
   - Codecs and packages installed via --enablerepo='fedora-multimedia' in 03-packages.sh
5. Excluded packages (base.toml [excluded])

This verifier derives the effective package payload from upstream Bluefin
and asserts that packages/bluefin.toml tracks all effective requirements.
"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
import urllib.request
from pathlib import Path
from typing import Any

BASE_TOML_URL = (
    "https://raw.githubusercontent.com/projectbluefin/bluefin/main/build_files/packages/base.toml"
)
PACKAGES_SH_URL = (
    "https://raw.githubusercontent.com/projectbluefin/bluefin/main/build_files/base/03-packages.sh"
)


def read_source(location: str) -> str:
    if location.startswith("http://") or location.startswith("https://"):
        req = urllib.request.Request(
            location, headers={"User-Agent": "utah-parity-checker"}
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.read().decode("utf-8")
    return Path(location).read_text(encoding="utf-8")


def expand_braces(text: str) -> list[str]:
    match = re.search(r"([^{}\s]*)\{([^{}]+)\}([^{}\s]*)", text)
    if not match:
        return [text]
    prefix, options, suffix = match.groups()
    results = []
    for opt in options.split(","):
        results.extend(expand_braces(f"{prefix}{opt}{suffix}"))
    return results


def derive_effective_payload(base_toml_text: str, packages_sh_text: str) -> dict[str, list[str]]:
    base_data = tomllib.loads(base_toml_text)

    # 1. Copr packages from 03-packages.sh (e.g. copr_install_isolated "ublue-os/packages" "uupd")
    copr_packages = []
    for m in re.finditer(
        r'copr_install_isolated\s+["\']([^"\']+)["\'](?:\s*\\\s*|\s+)["\']([^"\']+)["\']',
        packages_sh_text,
    ):
        copr_packages.extend(m.group(2).split())

    # 2. Extract dnf install arguments from 03-packages.sh
    lines = packages_sh_text.splitlines()
    in_install = False
    install_tokens = []
    for line in lines:
        stripped = line.strip()
        if not in_install:
            if re.search(r"\bdnf[0-9]?\s+.*install\b", stripped):
                in_install = True
                parts = re.split(r"\bdnf[0-9]?\s+.*install\s+", stripped, maxsplit=1)
                token_part = parts[1] if len(parts) > 1 else ""
                ends_cont = token_part.endswith("\\")
                token_part = token_part.rstrip("\\").strip()
                install_tokens.extend(token_part.split())
                if not ends_cont:
                    in_install = False
        else:
            ends_cont = stripped.endswith("\\")
            token_part = stripped.rstrip("\\").strip()
            install_tokens.extend(token_part.split())
            if not ends_cont:
                in_install = False

    external_pkgs = list(copr_packages)
    multimedia_pkgs = []
    skip_next = False
    current_repo = None

    for token in install_tokens:
        token = token.strip(" \"'")
        if skip_next:
            skip_next = False
            continue
        if token in ("-x", "--exclude"):
            skip_next = True
            continue
        if token.startswith("-x=") or token.startswith("--exclude="):
            continue
        if token.startswith("--enablerepo="):
            current_repo = token.split("=", 1)[1].strip(" \"'")
            continue
        if token.startswith("-"):
            continue
        if token.startswith('"${') or token.startswith("${"):
            continue

        expanded = expand_braces(token)
        for pkg in expanded:
            if current_repo == "tailscale-stable" or pkg == "tailscale":
                external_pkgs.append(pkg)
            elif current_repo == "fedora-multimedia" or any(
                m in pkg
                for m in (
                    "ffmpeg",
                    "gstreamer",
                    "lame",
                    "multimedia",
                    "libjxl",
                    "libfdk",
                    "libavcodec",
                )
            ):
                multimedia_pkgs.append(pkg)
            else:
                external_pkgs.append(pkg)

    payload: dict[str, list[str]] = {
        "fedora": sorted(set(base_data.get("fedora", {}).get("packages", []))),
    }

    # Version-specific additions (e.g. fedora_v42, fedora_v43, fedora_v44)
    for k in sorted(base_data.keys()):
        if k.startswith("fedora_v"):
            payload[k] = sorted(set(base_data[k].get("packages", [])))

    payload["external"] = sorted(set(external_pkgs))
    payload["multimedia"] = sorted(set(multimedia_pkgs))
    payload["multimedia_overrides"] = sorted(
        set(base_data.get("multimedia_overrides", {}).get("packages", []))
    )
    payload["excluded"] = sorted(set(base_data.get("excluded", {}).get("packages", [])))

    return payload


def format_toml(payload: dict[str, list[str]]) -> str:
    lines = [
        "# Machine-readable contract derived from Bluefin's effective package payload.",
        "#",
        "# Sources in projectbluefin/bluefin:",
        "#   - build_files/packages/base.toml (fedora, version-specific, overrides, excluded)",
        "#   - build_files/base/03-packages.sh (external and multimedia transactions)",
        "#",
        "# Sections:",
        "#   [fedora]                 base Fedora packages common to all supported versions",
        "#   [fedora_v*]              version-specific Fedora additions",
        "#   [external]               packages from external repositories/COPRs",
        "#   [multimedia]             codecs and multimedia packages from 03-packages.sh",
        "#   [multimedia_overrides]   packages synced from negativo17/fedora-multimedia",
        "#   [excluded]               packages removed from the base image",
        "#",
        "# Validated by scripts/check-parity.py on every CI run (`just check-parity`).",
        "",
    ]

    section_comments = {
        "fedora": "# Base packages from Fedora repos — common to all supported Fedora versions.",
        "fedora_v42": "# Fedora 42 additions",
        "fedora_v43": "# Fedora 43 additions",
        "fedora_v44": "# Fedora 44 additions",
        "external": "# External and COPR packages installed in Bluefin's package transaction.",
        "multimedia": "# Codecs and extra multimedia packages installed in Bluefin's transaction.",
        "multimedia_overrides": "# Packages synced from negativo17/fedora-multimedia repo.",
        "excluded": "# Packages removed from the base image — conflicts with or replaced by image content.",
    }

    for section_name, packages in payload.items():
        comment = section_comments.get(section_name)
        if comment:
            lines.append(comment)
        lines.append(f"[{section_name}]")
        lines.append("packages = [")
        for pkg in packages:
            lines.append(f'    "{pkg}",')
        lines.append("]")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify parity between Utah's Bluefin contract and upstream Bluefin."
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("packages/bluefin.toml"),
        help="Path to local Bluefin package contract (default: packages/bluefin.toml)",
    )
    parser.add_argument(
        "--upstream-manifest",
        default=BASE_TOML_URL,
        help="URL or path to Bluefin base.toml",
    )
    parser.add_argument(
        "--upstream-script",
        default=PACKAGES_SH_URL,
        help="URL or path to Bluefin 03-packages.sh",
    )
    parser.add_argument(
        "--write",
        "--update",
        action="store_true",
        help="Write derived effective payload to the local contract file",
    )
    args = parser.parse_args()

    base_toml_text = read_source(args.upstream_manifest)
    packages_sh_text = read_source(args.upstream_script)
    effective_payload = derive_effective_payload(base_toml_text, packages_sh_text)

    if args.write:
        formatted = format_toml(effective_payload)
        args.manifest.write_text(formatted, encoding="utf-8")
        print(f"Updated {args.manifest} from upstream Bluefin effective payload.")
        return 0

    if not args.manifest.exists():
        print(f"ERROR: {args.manifest} does not exist.", file=sys.stderr)
        return 1

    local_data = tomllib.loads(args.manifest.read_text(encoding="utf-8"))
    diff_found = False

    all_sections = sorted(set(effective_payload.keys()) | set(local_data.keys()))
    for section_name in all_sections:
        upstream_pkgs = set(effective_payload.get(section_name, []))
        local_pkgs = set(local_data.get(section_name, {}).get("packages", []))

        missing = sorted(upstream_pkgs - local_pkgs)
        unexpected = sorted(local_pkgs - upstream_pkgs)

        if missing or unexpected:
            diff_found = True
            print(f"Drift detected in section [{section_name}]:", file=sys.stderr)
            if missing:
                print(f"  Missing additions from upstream ({len(missing)}):", file=sys.stderr)
                for pkg in missing:
                    print(f"    + {pkg}", file=sys.stderr)
            if unexpected:
                print(f"  Unexpected packages in contract ({len(unexpected)}):", file=sys.stderr)
                for pkg in unexpected:
                    print(f"    - {pkg}", file=sys.stderr)

    if diff_found:
        print(
            "\nERROR: packages/bluefin.toml has drifted from Bluefin's effective payload.",
            file=sys.stderr,
        )
        print("Run `python3 scripts/check-parity.py --write` to update.", file=sys.stderr)
        return 1

    print("packages/bluefin.toml matches Bluefin's effective package payload.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
