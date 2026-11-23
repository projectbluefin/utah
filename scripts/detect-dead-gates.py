#!/usr/bin/env python3
"""TAP report of gates that never produce a passing run.

Green-criteria.yml declares gates. This tool asks the GitHub API what the most
recent run of each gate's job actually did, and reports the ones that are dead:

  * never ran;
  * last run failed;
  * last run is older than the criterion's freshness_sla_days (stale).

It writes TAP so it can drop into CI or be read by a human. The pure
classification (classify_gate) is importable and unit tested; the API call is
the only network boundary.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import urllib.request
from datetime import timezone
from pathlib import Path

import yaml

FAILURE_CONCLUSIONS = {"failure", "timedout", "action_required",
                       "startup_failure", "cancelled"}


def load_spec(root: Path) -> dict:
    with (root / ".github" / "green-criteria.yml").open() as fh:
        return yaml.safe_load(fh)


def classify_gate(summary: dict, sla_days) -> dict:
    """Classify one gate from its latest-run summary.

    summary: {"has_run": bool, "last_conclusion": str|None,
              "last_date": datetime|None}.
    Returns {"state": "healthy"|"failed"|"stale"|"never", "detail": str}.
    """
    if not summary.get("has_run"):
        return {"state": "never", "detail": "no run found"}
    conclusion = summary.get("last_conclusion")
    if conclusion in FAILURE_CONCLUSIONS:
        return {"state": "failed", "detail": f"last run: {conclusion}"}
    last_date = summary.get("last_date")
    if sla_days is not None and last_date is not None:
        age = (dt.datetime.now(timezone.utc) - last_date).days
        if age > sla_days:
            return {"state": "stale",
                    "detail": f"last run {age}d ago, sla {sla_days}d"}
    return {"state": "healthy",
            "detail": f"last run: {conclusion}"}


def _http_get_json(url: str, token: str):
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def latest_run_summary(token, owner, repo, gate) -> dict:
    """The latest run of a gate's workflow and the conclusion of its job."""
    url = (f"https://api.github.com/repos/{owner}/{repo}"
           f"/actions/workflows/{gate['workflow']}/runs?per_page=1")
    data = _http_get_json(url, token)
    runs = data.get("workflow_runs", [])
    if not runs:
        return {"has_run": False, "last_conclusion": None, "last_date": None}
    run = runs[0]
    job_conclusion = None
    job_date = None
    if run.get("status") == "completed":
        jobs_url = run.get("jobs_url", "").replace("{?page,per_page}", "")
        jobs = _http_get_json(jobs_url + "?per_page=1", token).get("jobs", [])
        for job in jobs:
            if job.get("name") == gate.get("job"):
                job_conclusion = job.get("conclusion")
                job_date = run.get("started_at")
                break
    return {
        "has_run": True,
        "last_conclusion": job_conclusion,
        "last_date": _parse(job_date),
    }


def _parse(value):
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def emit_tap(reports):
    """Render the TAP 13 report for ``reports`` and return it as a string."""
    lines = ["TAP version 13", f"1..{len(reports)}"]
    for i, r in enumerate(reports, 1):
        ok = r["state"] == "healthy"
        lines.append(f"{'ok' if ok else 'not ok'} {i} - {r['id']}: {r['detail']}")
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default="projectbluefin/utah")
    parser.add_argument("--token", default=None)
    parser.add_argument("--dry-run", action="store_true",
                        help="Print each gate's classification as never "
                             "(no network).")
    args = parser.parse_args(argv)

    spec = load_spec(Path(".").resolve())
    owner, repo = args.repo.split("/", 1)

    reports = []
    for crit in spec.get("criteria", []):
        for gate in crit.get("gates", []):
            if args.dry_run or not args.token:
                reports.append({"id": crit["id"], "state": "never",
                                "detail": "dry-run (no API)"})
                continue
            summary = latest_run_summary(args.token, owner, repo, gate)
            classified = classify_gate(summary, crit.get("freshness_sla_days"))
            classified["id"] = crit["id"]
            reports.append(classified)

    text = emit_tap(reports)
    print(text)
    return 0 if all(r["state"] == "healthy" for r in reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())
