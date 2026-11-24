#!/usr/bin/env python3
"""Fail when a pushed image's revision label is not the commit that built it.

`just build-ghcr` checks `org.opencontainers.image.revision` on the local image
before the push (#1400). The bytes that reach ghcr.io are produced after that
check -- the reusable build rechunks the image and pushes the result -- so this
reads the label back from the registry, by the exact digest each build call
reported, and refuses the run if any pushed image names another commit (#1468).

Usage: verify-pushed-revision.py <expected-sha> <owner> <digests-json>...

Each <digests-json> is a reusable-build `digests` output,
{"<image>": {"<platform>": "sha256:..."}}. An empty string is a build call that
was skipped and reported nothing; at least one image must be reported overall.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from typing import Callable, Iterable

LABEL = "org.opencontainers.image.revision"
SHA = re.compile(r"[0-9a-f]{40}")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
NAME = re.compile(r"[a-z0-9][a-z0-9._-]*")


def pushed_refs(digest_maps: Iterable[str], owner: str) -> list[str]:
    """Immutable registry refs for every image the build calls reported."""
    refs = set()
    for raw in digest_maps:
        if not raw.strip():
            continue
        for name, platforms in json.loads(raw).items():
            if not NAME.fullmatch(name):
                raise ValueError(f"unexpected image name {name!r}")
            for platform, digest in platforms.items():
                if not DIGEST.fullmatch(digest):
                    raise ValueError(f"{name} {platform}: not a sha256 digest: {digest!r}")
                refs.add(f"ghcr.io/{owner}/{name}@{digest}")
    if not refs:
        raise ValueError("the build reported no pushed images to verify")
    return sorted(refs)


def skopeo_labels(ref: str) -> dict[str, str]:
    """The config labels of the manifest at `ref`, read from the registry."""
    out = subprocess.run(
        ["skopeo", "inspect", "--no-tags", f"docker://{ref}"],
        check=True, capture_output=True, text=True).stdout
    return json.loads(out).get("Labels") or {}


def mismatches(refs: Iterable[str], expected: str,
               labels: Callable[[str], dict[str, str]]) -> list[str]:
    """One message per pushed image whose revision label is not `expected`."""
    problems = []
    for ref in refs:
        revision = labels(ref).get(LABEL, "")
        if revision != expected:
            problems.append(f"{ref}: {LABEL} is {revision or '<missing>'!r}, "
                            f"expected the building commit {expected}")
        else:
            print(f"{ref}: {LABEL} = {revision}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("expected_sha")
    parser.add_argument("owner")
    parser.add_argument("digests", nargs="+")
    args = parser.parse_args(argv)
    if not SHA.fullmatch(args.expected_sha):
        print(f"::error::expected sha must be a full 40-hex commit: {args.expected_sha!r}",
              file=sys.stderr)
        return 1
    try:
        refs = pushed_refs(args.digests, args.owner.lower())
    except ValueError as error:
        print(f"::error::{error}", file=sys.stderr)
        return 1
    problems = mismatches(refs, args.expected_sha, skopeo_labels)
    for problem in problems:
        print(f"::error title=pushed image revision mismatch::{problem}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
