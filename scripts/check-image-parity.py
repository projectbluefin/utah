#!/usr/bin/env python3
"""Compare what this image installed against what Bluefin's image installed.

packages/bluefin.toml is a copy of the list Bluefin *asks* for. It says nothing
about what Bluefin's base image already had before that list was applied, and
that is where the parity gaps that reach users have been hiding: Bluefin's
Silverblue base ships glibc-all-langpacks, linux-firmware and the iwlwifi
blobs, so its package list never names them, so Utah's contract never named
them either, so Hummingbird's much smaller base never supplied them (#97,
#114). A list-to-list comparison cannot see any of that. Only the two images
can.

Bluefin's side needs no pull. Every Bluefin image is rechunked, and the
rechunker writes the complete installed inventory -- every RPM name and its
version -- into the manifest annotation dev.hhd.rechunk.info. One anonymous
registry GET returns it. Utah's side is rpm -qa, run where this script runs:
inside the image build, after the last package step.

The comparison is by name. A name Bluefin has that this image does not is a
gap, unless packages/utah.toml already documents it under [unavailable] or
packages/parity-exceptions.toml explains it. Without --strict the result is a
report; with --strict an unexplained gap fails the build.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import subprocess
import sys
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

DEFAULT_BLUEFIN_IMAGE = "ghcr.io/ublue-os/bluefin:stable"
RECHUNK_ANNOTATION = "dev.hhd.rechunk.info"
MANIFEST_TYPES = ", ".join(
    [
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    ]
)

Fetch = Callable[[str, dict[str, str]], bytes]


def http_get(url: str, headers: dict[str, str]) -> bytes:
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def parse_image_ref(ref: str) -> tuple[str, str, str]:
    """Split registry/repository:tag (or @digest) into its three parts."""
    if "@" in ref:
        name, reference = ref.split("@", 1)
    elif ":" in ref.rsplit("/", 1)[-1]:
        name, reference = ref.rsplit(":", 1)
    else:
        name, reference = ref, "latest"
    registry, _, repository = name.partition("/")
    if not repository or ("." not in registry and ":" not in registry):
        raise ValueError(f"image reference needs an explicit registry: {ref}")
    return registry, repository, reference


def bluefin_inventory(ref: str, fetch: Fetch = http_get) -> dict[str, str]:
    """Package name -> version-release for a rechunked image, read from its manifest."""
    registry, repository, reference = parse_image_ref(ref)
    # ghcr.io grants anonymous pull tokens for public packages; other
    # registries answer the manifest request directly.
    headers = {"Accept": MANIFEST_TYPES}
    if registry == "ghcr.io":
        token_url = f"https://ghcr.io/token?scope=repository:{repository}:pull"
        token = json.loads(fetch(token_url, {}))["token"]
        headers["Authorization"] = f"Bearer {token}"
    base = f"https://{registry}/v2/{repository}/manifests/"
    manifest = json.loads(fetch(base + urllib.parse.quote(reference, safe=":"), headers))
    if "manifests" in manifest:
        # A multi-arch index: the annotation lives on the per-arch manifest.
        for entry in manifest["manifests"]:
            platform = entry.get("platform", {})
            if platform.get("architecture") == "amd64" and platform.get("os") == "linux":
                manifest = json.loads(fetch(base + entry["digest"], headers))
                break
        else:
            raise ValueError(f"{ref}: no linux/amd64 manifest in the index")
    annotation = manifest.get("annotations", {}).get(RECHUNK_ANNOTATION)
    if not annotation:
        raise ValueError(
            f"{ref} carries no {RECHUNK_ANNOTATION} annotation; "
            "only a rechunked image publishes its inventory this way"
        )
    packages = json.loads(annotation).get("packages")
    if not isinstance(packages, dict) or not packages:
        raise ValueError(f"{ref}: {RECHUNK_ANNOTATION} has no package inventory")
    return dict(packages)


def installed_inventory() -> dict[str, str]:
    out = subprocess.run(
        ["rpm", "-qa", "--qf", "%{NAME}\t%{VERSION}-%{RELEASE}\n"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return parse_rpm_list(out)


def parse_rpm_list(text: str) -> dict[str, str]:
    """Accept either NAME<tab>VERSION lines or bare NAME lines."""
    inventory: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("gpg-pubkey"):
            continue
        name, _, version = line.partition("\t")
        inventory[name] = version
    return inventory


@dataclass
class Exception_:
    pattern: str
    reason: str


def load_exceptions(path: Path) -> list[Exception_]:
    if not path.exists():
        return []
    data = tomllib.loads(path.read_text())
    exceptions = []
    for entry in data.get("exception", []):
        if not entry.get("pattern") or not entry.get("reason"):
            raise ValueError(f"{path}: every [[exception]] needs a pattern and a reason")
        exceptions.append(Exception_(entry["pattern"], entry["reason"].strip()))
    return exceptions


def unavailable_names(overlay: Path) -> set[str]:
    if not overlay.exists():
        return set()
    data = tomllib.loads(overlay.read_text())
    return set(data.get("unavailable", {}).get("packages", []))


@dataclass
class Comparison:
    missing: list[str] = field(default_factory=list)
    explained: list[tuple[str, str]] = field(default_factory=list)
    extra: list[str] = field(default_factory=list)


def compare(
    bluefin: dict[str, str],
    utah: dict[str, str],
    unavailable: set[str],
    exceptions: list[Exception_],
) -> Comparison:
    result = Comparison()
    for name in sorted(bluefin):
        if name in utah:
            continue
        if name in unavailable:
            result.explained.append((name, "documented under [unavailable] in utah.toml"))
            continue
        for exc in exceptions:
            if fnmatch.fnmatchcase(name, exc.pattern):
                result.explained.append((name, exc.reason))
                break
        else:
            result.missing.append(name)
    result.extra = sorted(name for name in utah if name not in bluefin)
    return result


def render(ref: str, bluefin: dict[str, str], utah: dict[str, str], result: Comparison) -> str:
    lines = [
        f"image parity against {ref}",
        f"  Bluefin installs {len(bluefin)} packages, this image {len(utah)}",
        f"  in Bluefin, not here, unexplained: {len(result.missing)}",
        f"  in Bluefin, not here, explained:   {len(result.explained)}",
        f"  here, not in Bluefin:              {len(result.extra)}",
    ]
    if result.missing:
        lines.append("")
        lines.append("UNEXPLAINED -- in Bluefin's image and not in this one:")
        lines += [f"  {name}  ({bluefin[name]})" for name in result.missing]
    if result.explained:
        lines.append("")
        lines.append("explained:")
        lines += [f"  {name}: {reason}" for name, reason in result.explained]
    if result.extra:
        lines.append("")
        lines.append("only here (informational; Hummingbird's base and Utah's overlay):")
        lines += [f"  {name}" for name in result.extra]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--bluefin-image", default=DEFAULT_BLUEFIN_IMAGE)
    parser.add_argument(
        "--bluefin-inventory",
        type=Path,
        help="JSON {name: version} to use instead of reading the registry",
    )
    parser.add_argument(
        "--rpm-list",
        type=Path,
        help="NAME or NAME<tab>VERSION per line, instead of running rpm -qa here",
    )
    parser.add_argument("--overlay", type=Path, default=Path("/usr/share/utah/utah.toml"))
    parser.add_argument(
        "--exceptions", type=Path, default=Path("/usr/share/utah/parity-exceptions.toml")
    )
    parser.add_argument("--report", type=Path, help="also write the report here")
    parser.add_argument(
        "--strict", action="store_true", help="exit 1 when an unexplained gap remains"
    )
    args = parser.parse_args(argv)

    try:
        if args.bluefin_inventory:
            bluefin = json.loads(args.bluefin_inventory.read_text())
        else:
            bluefin = bluefin_inventory(args.bluefin_image)
    except (urllib.error.URLError, ValueError, KeyError, OSError) as exc:
        # Without Bluefin's inventory there is nothing to compare. That is a
        # failure of the check, not evidence about the image, so it is fatal
        # only when the check is asked to gate.
        message = f"could not read Bluefin's inventory from {args.bluefin_image}: {exc}"
        print(message, file=sys.stderr)
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(message + "\n")
        return 1 if args.strict else 0

    utah = parse_rpm_list(args.rpm_list.read_text()) if args.rpm_list else installed_inventory()
    result = compare(bluefin, utah, unavailable_names(args.overlay), load_exceptions(args.exceptions))
    report = render(args.bluefin_image, bluefin, utah, result)
    print(report, end="")
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(report)
    if result.missing and args.strict:
        print(f"{len(result.missing)} package(s) Bluefin ships are missing and unexplained", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
