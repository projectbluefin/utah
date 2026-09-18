#!/usr/bin/env python3
"""Fail closed when selecting immutable images from one trusted build run."""
import argparse
import json
from pathlib import Path
import re
import subprocess


def resolve(run, expected, artifact_dir, repository):
    if (run.get("conclusion") != "success" or run.get("head_branch") != "testing"
            or run.get("event") not in {"push", "workflow_dispatch", "schedule"}
            or run.get("repository", {}).get("full_name") != repository
            or run.get("head_repository", {}).get("full_name") != repository
            or run.get("path") != ".github/workflows/build.yml"
            or not re.fullmatch(r"[0-9a-f]{40}", run.get("head_sha", ""))):
        raise ValueError("not a successful trusted testing image build")
    images = {}
    for path in Path(artifact_dir).rglob("*.txt"):
        for line in path.read_text().splitlines():
            if not line:
                continue
            if "|" in line:
                name, arch, digest = line.split("|")
                if arch != "amd64":
                    raise ValueError("unexpected architecture")
            else:
                name, digest = line.split("=")
            if name not in expected or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
                raise ValueError("unexpected image name or digest")
            if name in images and images[name] != digest:
                raise ValueError("conflicting image digests")
            images[name] = digest
    if set(images) != set(expected):
        raise ValueError("build artifacts do not cover the configured flavor set")
    owner = repository.split("/")[0].lower()
    return {"include": [{"image": name, "digest": images[name],
                         "ref": f"ghcr.io/{owner}/{name}@{images[name]}"}
                        for name in expected]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("run")
    parser.add_argument("artifacts")
    parser.add_argument("repository")
    args = parser.parse_args()
    expected = [item["image"] for item in json.loads(subprocess.check_output(
        ["python3", "scripts/flavors.py", "images"], text=True))]
    print(json.dumps(resolve(json.loads(Path(args.run).read_text()), expected,
                             args.artifacts, args.repository)))
