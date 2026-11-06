#!/usr/bin/env python3
"""Open an issue when `:testing` has stopped publishing, and close it when it resumes.

`:testing` advances only when post-testing-e2e passes the whole LUKS matrix, so
a broken gate freezes it silently: every build stays green and nothing says the
tags have not moved. Between 2026-10-30 and 11-06 a missing provenance
attestation did exactly that for a week, and nobody was told. This makes it
loud: on a schedule, it reads each flavor's `:testing` image creation time from
the registry, and when any is older than the threshold it opens (or refreshes)
one issue the hive picks up, with the latest post-testing-e2e failure attached.
When every flavor is fresh again it closes that issue.

Usage:
  testing-freshness.py [--max-age-hours N] [--dry-run]

Environment: GH_TOKEN (issues: write, actions: read), GITHUB_REPOSITORY.
"""

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MARKER = "<!-- utah:testing-freshness -->"
TITLE = ":testing has not published for over {hours} hours"
LABELS = ["kind/bug", "area/ci", "priority/critical-urgent"]
REGISTRY = "ghcr.io/projectbluefin"


def run(*cmd: str, check: bool = True) -> str:
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if check and result.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} failed: {result.stderr.strip()}")
    return result.stdout


def images() -> list[str]:
    out = run(sys.executable, str(ROOT / "scripts/flavors.py"), "images")
    return [entry["image"] for entry in json.loads(out)]


def parse_created(value: str) -> dt.datetime:
    # skopeo prints RFC 3339 with nanoseconds ("2026-10-30T10:14:07.123456789Z");
    # fromisoformat accepts at most microseconds, so trim the fraction to six.
    value = re.sub(r"\.(\d{6})\d+", r".\1", value.replace("Z", "+00:00"))
    return dt.datetime.fromisoformat(value)


def created(image: str) -> dt.datetime | None:
    try:
        out = run("skopeo", "inspect", "--no-tags", f"docker://{REGISTRY}/{image}:testing")
        return parse_created(json.loads(out)["Created"])
    except (RuntimeError, KeyError, ValueError) as error:
        print(f"warning: cannot read {image}:testing: {error}", file=sys.stderr)
        return None


def stale(ages: dict[str, dt.datetime | None], now: dt.datetime, max_age: dt.timedelta):
    """Flavors whose :testing is older than max_age. An unreadable tag counts as stale."""
    return {image: when for image, when in ages.items() if when is None or now - when > max_age}


def latest_e2e(repo: str) -> str:
    """One line on the newest post-testing-e2e run and its failed jobs, for the issue body."""
    try:
        runs = json.loads(run("gh", "run", "list", "-R", repo, "--workflow", "post-testing-e2e.yml",
                              "--limit", "1", "--json", "databaseId,conclusion,createdAt,url"))
    except RuntimeError as error:
        return f"Could not list post-testing-e2e runs: {error}"
    if not runs:
        return "No post-testing-e2e run found."
    latest = runs[0]
    line = f"Latest post-testing-e2e: [{latest['conclusion'] or 'in progress'}]({latest['url']}) at {latest['createdAt']}."
    try:
        jobs = json.loads(run("gh", "run", "view", str(latest["databaseId"]), "-R", repo, "--json", "jobs"))["jobs"]
    except RuntimeError:
        return line
    failed = sorted({j["name"] for j in jobs if j.get("conclusion") == "failure"})
    if failed:
        line += "\n\nFailed jobs:\n" + "\n".join(f"- `{name}`" for name in failed)
    return line


def body(ages, stale_set, now, max_age_hours, e2e_line) -> str:
    rows = []
    for image, when in ages.items():
        if when is None:
            rows.append(f"| `{image}` | unreadable | **stale** |")
        else:
            age = (now - when).total_seconds() / 3600
            state = "**stale**" if image in stale_set else "fresh"
            rows.append(f"| `{image}` | {when:%Y-%m-%d %H:%M} UTC ({age:.0f} h ago) | {state} |")
    return "\n".join([
        MARKER,
        f"`:testing` should advance daily. At least one flavor's `:testing` image is older than "
        f"{max_age_hours} hours, so post-testing-e2e has stopped promoting.",
        "",
        "| Image | `:testing` created | |",
        "| --- | --- | --- |",
        *rows,
        "",
        e2e_line,
        "",
        "**What to do:** find out why post-testing-e2e does not reach `promote-to-testing` "
        "(see `docs/skills/ci-workflows/references/iso-e2e.md`), fix the cause, and confirm that "
        "the next run promotes. This issue closes itself once every flavor is fresh again.",
        "",
        f"_Refreshed by `scripts/testing-freshness.py` at {now:%Y-%m-%d %H:%M} UTC._",
    ])


def open_issue(repo: str) -> dict | None:
    issues = json.loads(run("gh", "issue", "list", "-R", repo, "--state", "open", "--limit", "100",
                            "--search", "in:body \"utah:testing-freshness\"",
                            "--json", "number,body"))
    return next((i for i in issues if MARKER in i["body"]), None)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-age-hours", type=int, default=36)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    repo = os.environ.get("GITHUB_REPOSITORY", "projectbluefin/utah")
    now = dt.datetime.now(dt.timezone.utc)
    max_age = dt.timedelta(hours=args.max_age_hours)

    ages = {image: created(image) for image in images()}
    stale_set = stale(ages, now, max_age)
    existing = None if args.dry_run else open_issue(repo)

    if not stale_set:
        print(f"every :testing image is fresh (max age {args.max_age_hours} h)")
        if existing:
            run("gh", "issue", "close", str(existing["number"]), "-R", repo, "--reason", "completed",
                "--comment", "Every flavor's `:testing` image is fresh again; closing.")
            print(f"closed #{existing['number']}")
        return 0

    text = body(ages, stale_set, now, args.max_age_hours, latest_e2e(repo))
    if args.dry_run:
        print(text)
        return 0
    if existing:
        run("gh", "issue", "edit", str(existing["number"]), "-R", repo, "--body", text,
            "--title", TITLE.format(hours=args.max_age_hours))
        print(f"refreshed #{existing['number']}")
    else:
        cmd = ["gh", "issue", "create", "-R", repo, "--title", TITLE.format(hours=args.max_age_hours),
               "--body", text]
        for label in LABELS:
            cmd += ["--label", label]
        print(run(*cmd).strip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
