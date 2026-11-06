#!/usr/bin/env python3
"""Open an issue when `:testing` has stopped advancing, and close it when it moves again.

`:testing` moves only when post-testing-e2e reaches its `promote-to-testing`
job and the "Move :testing to the tested digests" step runs. A broken gate
freezes the tag while every build stays green: between 2026-10-30 and 11-06 a
missing provenance attestation did exactly that for a week, and nothing said
so. This reads the time of the last run that actually moved the tag. When that
is older than the threshold, it opens (or refreshes) one issue for the hive,
with the newest post-testing-e2e run and its failed jobs attached. When the tag
moves again, it closes the issue.

The image's own `created` field is not used: builds set it from
SOURCE_DATE_EPOCH (the last build-input commit), so a quiet weekend of green
promotions would read as stale, and a promotion after a long freeze as fresh.

Usage:
  testing-freshness.py [--max-age-hours N] [--dry-run]

Environment: GH_TOKEN (issues: write, actions: read), GITHUB_REPOSITORY.
"""

import argparse
import datetime as dt
import json
import os
import subprocess
import sys

MARKER = "<!-- utah:testing-freshness -->"
TITLE = ":testing has not advanced for over {hours} hours"
LABELS = ["kind/bug", "area/ci", "priority/critical-urgent"]
WORKFLOW = "post-testing-e2e.yml"
PROMOTE_JOB = "promote-to-testing"
MOVE_STEP = "Move :testing to the tested digests"
RUNS_SCANNED = 60


def gh(*args: str):
    result = subprocess.run(["gh", *args], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)} failed: {result.stderr.strip()}")
    return json.loads(result.stdout) if result.stdout.strip() else None


def parse_time(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def moved_at(jobs: list[dict]) -> dt.datetime | None:
    """When this run moved :testing: the move step's completion, or None if it never ran."""
    for job in jobs:
        if job.get("name") != PROMOTE_JOB or job.get("conclusion") != "success":
            continue
        for step in job.get("steps") or []:
            if step.get("name") == MOVE_STEP and step.get("conclusion") == "success":
                return parse_time(step["completed_at"])
    return None


def last_promotion(repo: str, runs: list[dict], jobs_of) -> tuple[dt.datetime | None, dict | None]:
    """Newest run (of `runs`, newest first) whose move step ran, and when it moved the tag."""
    for run in runs:
        if run.get("status") != "completed":
            continue
        when = moved_at(jobs_of(run["id"]))
        if when:
            return when, run
    return None, None


def is_stale(when: dt.datetime | None, now: dt.datetime, max_age: dt.timedelta) -> bool:
    """No promotion found among the scanned runs counts as stale, never as fresh."""
    return when is None or now - when > max_age


def describe_latest(run: dict | None, jobs: list[dict]) -> str:
    if not run:
        return "No post-testing-e2e run found."
    line = (f"Latest post-testing-e2e: [{run.get('conclusion') or run.get('status')}]"
            f"({run['html_url']}) started {run['created_at']}.")
    failed = sorted({j["name"] for j in jobs if j.get("conclusion") == "failure"})
    if failed:
        line += "\n\nFailed jobs:\n" + "\n".join(f"- `{name}`" for name in failed)
    return line


def body(when, promoted_run, now, max_age_hours, latest_line) -> str:
    if when:
        moved = (f"`:testing` last moved {when:%Y-%m-%d %H:%M} UTC "
                 f"({(now - when).total_seconds() / 3600:.0f} h ago), in [this run]({promoted_run['html_url']}).")
    else:
        moved = f"No promotion found in the last {RUNS_SCANNED} post-testing-e2e runs."
    return "\n".join([
        MARKER,
        f"`:testing` should advance at least daily. It has not moved for more than {max_age_hours} hours, "
        "so post-testing-e2e is not reaching its promotion step.",
        "",
        moved,
        "",
        latest_line,
        "",
        "**What to do:** find out why post-testing-e2e does not promote "
        "(see `docs/skills/ci-workflows/references/iso-e2e.md`), fix the cause, and confirm the next run "
        "moves the tag. This issue closes itself once `:testing` moves again.",
        "",
        f"_Refreshed by `scripts/testing-freshness.py` at {now:%Y-%m-%d %H:%M} UTC._",
    ])


def open_issue(repo: str) -> dict | None:
    issues = gh("issue", "list", "-R", repo, "--state", "open", "--limit", "100",
                "--search", "in:body \"utah:testing-freshness\"", "--json", "number,body")
    return next((i for i in issues or [] if MARKER in i["body"]), None)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-age-hours", type=int, default=36)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    repo = os.environ.get("GITHUB_REPOSITORY", "projectbluefin/utah")
    now = dt.datetime.now(dt.timezone.utc)

    runs = gh("api", f"repos/{repo}/actions/workflows/{WORKFLOW}/runs?per_page={RUNS_SCANNED}")["workflow_runs"]
    cache: dict[int, list[dict]] = {}

    def jobs_of(run_id: int) -> list[dict]:
        if run_id not in cache:
            cache[run_id] = gh("api", f"repos/{repo}/actions/runs/{run_id}/jobs?per_page=100")["jobs"]
        return cache[run_id]

    when, promoted_run = last_promotion(repo, runs, jobs_of)
    existing = None if args.dry_run else open_issue(repo)

    if not is_stale(when, now, dt.timedelta(hours=args.max_age_hours)):
        print(f":testing last moved {when:%Y-%m-%d %H:%M} UTC; fresh (max age {args.max_age_hours} h)")
        if existing:
            gh("issue", "close", str(existing["number"]), "-R", repo, "--reason", "completed",
               "--comment", f"`:testing` moved again at {when:%Y-%m-%d %H:%M} UTC; closing.")
            print(f"closed #{existing['number']}")
        return 0

    latest = runs[0] if runs else None
    text = body(when, promoted_run, now, args.max_age_hours,
                describe_latest(latest, jobs_of(latest["id"]) if latest else []))
    if args.dry_run:
        print(text)
        return 0
    title = TITLE.format(hours=args.max_age_hours)
    if existing:
        gh("issue", "edit", str(existing["number"]), "-R", repo, "--title", title, "--body", text)
        print(f"refreshed #{existing['number']}")
    else:
        cmd = ["issue", "create", "-R", repo, "--title", title, "--body", text]
        for label in LABELS:
            cmd += ["--label", label]
        result = subprocess.run(["gh", *cmd], capture_output=True, text=True, check=True)
        print(result.stdout.strip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
