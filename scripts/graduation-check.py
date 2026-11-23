#!/usr/bin/env python3
"""Weekly graduation check for projectbluefin/utah.

Green-criteria.yml marks some gates advisory: evidence that is not yet trusted
to stop a release. Each week this script measures every advisory gate against
the graduation bar and reports the ones that have passed long enough to become
blocking. It never changes the enforcement field -- it only recommends.

A gate graduates when, over the most recent runs of its workflow:

  * it has passed in every run since its last failure
    (required_consecutive_passes, one per criterion); and
  * its failures are a small share of recent runs
    (maximum_failure_rate, one per criterion).

The pure grading logic (grade_runs) is importable and unit tested; the GitHub
API calls below are the only network boundary.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import yaml

# Conclusions that count as a failure for the failure-rate share.
FAILURE_CONCLUSIONS = {"failure", "timedout", "action_required",
                       "startup_failure", "cancelled"}
# Conclusions that are neither a pass nor a failure.
NOISE_CONCLUSIONS = {"skipped", "neutral"}


def load_spec(root: Path) -> dict:
    with (root / ".github" / "green-criteria.yml").open() as fh:
        return yaml.safe_load(fh)


def graduation_bar(graduation: dict, crit: dict) -> dict:
    """The bar for a criterion, with per-criterion overrides applied."""
    bar = dict(graduation)
    bar.update(crit.get("graduation", {}))
    return bar


def grade_runs(runs, bar: dict) -> dict:
    """Grade newest-first run results against one graduation bar.

    Each run is a dict with ``status`` ("completed" or not) and ``conclusion``
    (GitHub's conclusion string). Returns the metrics and whether the gate is
    ready to graduate.
    """
    completed = [r for r in runs if r.get("status") == "completed"]
    lookback = int(bar.get("lookback_runs", 20))
    needed = int(bar.get("required_consecutive_passes", 3))
    max_rate = float(bar.get("maximum_failure_rate", 0.10))

    consecutive = 0
    for r in completed:  # newest first
        if r.get("conclusion") == "success":
            consecutive += 1
        else:
            break

    window = completed[:lookback]
    failures = sum(
        1 for r in window
        if r.get("conclusion") in FAILURE_CONCLUSIONS
    )
    total = len(window)
    failure_rate = failures / total if total else 0.0

    return {
        "ready": consecutive >= needed and failure_rate <= max_rate,
        "consecutive_passes": consecutive,
        "needed": needed,
        "failure_rate": round(failure_rate, 3),
        "max_rate": max_rate,
        "runs_seen": total,
    }


def _http_get_json(url: str, token: str):
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def _run_gate_conclusion(token, run: dict, gate_job: str):
    """The conclusion of one gate job within a workflow run.

    A gate job's real GitHub name may carry a matrix suffix such as
    ``e2e (${{ matrix.image }})``; match on the base name so the gate is still
    found. Returns the first non-success conclusion, or None.
    """
    jobs_url = run.get("jobs_url", "").replace("{?page,per_page}", "")
    data = _http_get_json(jobs_url + "?per_page=100", token)
    for job in data.get("jobs", []):
        name = job.get("name", "")
        if name == gate_job or name.startswith(gate_job):
            conclusion = job.get("conclusion")
            if conclusion != "success":
                return conclusion
    return None


def fetch_workflow_runs(token, owner, repo, workflow, per_page: int,
                        gate_job: str):
    """Newest-first recent runs of a workflow, reduced to the gate job's
    conclusion. Returns a list of run dicts.

    ``workflow`` is a path under ``.github/workflows``; only the file name is
    valid in a workflow URL (the full path is a 404).
    """
    url = (f"https://api.github.com/repos/{owner}/{repo}"
           f"/actions/workflows/{Path(workflow).name}/runs?per_page={per_page}")
    data = _http_get_json(url, token)
    runs = []
    for run in data.get("workflow_runs", []):
        runs.append({
            "status": run.get("status"),
            "conclusion": _run_gate_conclusion(token, run, gate_job),
            "started_at": run.get("started_at"),
            "run_id": run.get("id"),
        })
    return runs


def _parse_date(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def report_for(crit, runs, bar) -> dict:
    metrics = grade_runs(runs, bar)
    metrics["id"] = crit.get("id")
    metrics["last_run"] = _parse_date((runs[0] if runs else {}).get("started_at"))
    return metrics


def print_report(owner, repo, spec, reports):
    graduation = spec.get("graduation", {})
    print(f"Graduation report for {owner}/{repo}")
    print(f"lookback {graduation.get('lookback_runs', 20)} runs\n")

    ready = [r for r in reports if r["ready"]]
    for r in reports:
        status = "READY to promote" if r["ready"] else "not ready"
        print(f"{status}: {r['id']}")
        print(f"  consecutive_passes: {r['consecutive_passes']} "
              f"(need {r['needed']})")
        print(f"  failure_rate: {r['failure_rate']} (max {r['max_rate']})")
        last = r["last_run"].isoformat() if r["last_run"] else "unknown"
        print(f"  last_run: {last}\n")

    if ready:
        names = ", ".join(r["id"] for r in ready)
        print(f"Recommend promoting to blocking: {names}")
    else:
        print("No advisory gate has met the graduation bar yet.")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default="projectbluefin/utah")
    parser.add_argument("--token", default=None,
                        help="GitHub token (or set GH_TOKEN). Needed for "
                             "network access; without it only the local "
                             "grading is validated.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Load green-criteria.yml and print the bar per "
                             "advisory gate without calling the API.")
    args = parser.parse_args(argv)

    root = Path(".").resolve()
    spec = load_spec(root)
    owner, repo = args.repo.split("/", 1)
    graduation = spec.get("graduation", {})

    reports = []
    for crit in spec.get("criteria", []):
        if crit.get("enforcement") != "advisory":
            continue
        gates = crit.get("gates", [])
        if not gates:
            continue
        gate = gates[0]
        bar = graduation_bar(graduation, crit)
        if args.dry_run or not args.token:
            print(f"dry-run: {crit['id']} bar="
                  f"consecutive>={bar['required_consecutive_passes']}, "
                  f"failure_rate<={bar['maximum_failure_rate']}")
            continue
        runs = fetch_workflow_runs(
            args.token, owner, repo, gate["workflow"],
            int(bar["lookback_runs"]), gate["job"],
        )
        reports.append(report_for(crit, runs, bar))

    if reports:
        print_report(owner, repo, spec, reports)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
