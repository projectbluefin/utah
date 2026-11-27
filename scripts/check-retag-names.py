#!/usr/bin/env python3
"""Check release-history names against the pinned reusable workflow."""

import importlib.util
import sys
import urllib.error
import urllib.request
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("release_trigger", ROOT / "scripts/release-trigger.py")
release_trigger = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release_trigger)


def fetch_workflow(url):
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=30) as response:
                return response.read().decode("utf-8")
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            if attempt == 2:
                raise RuntimeError(f"could not fetch {url}: {error}") from error


def main():
    caller = yaml.safe_load((ROOT / ".github/workflows/execute-release.yml").read_text())
    uses = caller["jobs"]["execute"]["uses"]
    location, commit = uses.rsplit("@", 1)
    owner, repo, path = location.split("/", 2)
    url = f"https://raw.githubusercontent.com/{owner}/{repo}/{commit}/{path}"
    try:
        workflow = yaml.safe_load(fetch_workflow(url))
    except RuntimeError as error:
        print(error, file=sys.stderr)
        return 2

    job_name = release_trigger.RETAG_JOB.removeprefix("execute / ")
    jobs = [job for job in workflow["jobs"].values() if job.get("name") == job_name]
    if not jobs:
        print(f"missing job {job_name!r} at {commit}", file=sys.stderr)
        return 1
    if not any(step.get("name") == release_trigger.RETAG_STEP
               for job in jobs for step in job.get("steps", [])):
        print(f"missing step {release_trigger.RETAG_STEP!r} in job {job_name!r} at {commit}", file=sys.stderr)
        return 1
    print(f"release retag names verified at {commit}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
