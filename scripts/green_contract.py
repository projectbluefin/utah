#!/usr/bin/env python3
"""Validate green-criteria.yml against the running Utah repository.

This is the `just check` gate for the "definition of green" contract. It reads
.github/green-criteria.yml and the workflow files that gates point at, and it
fails when a declared gate does not really enforce anything:

  * the workflow file, job or script named by a gate does not exist;
  * the workflow is not reachable from any active trigger
    (never_tested_is_not_green);
  * a *blocking* gate's job is not in the promotion job's `needs`;
  * a *blocking* gate's job can fail silently with `continue-on-error`
    (skipped_is_not_green);
  * a scheduled gate's cadence is slower than its freshness_sla_days
    (stale_is_not_green).

It does not touch the network and does not build anything, so it is safe to run
in `just check` on every commit.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

import yaml

PROMOTE_JOB_DEFAULT = "promote-to-testing"
ACTIVE_TRIGGERS = (
    "schedule",
    "push",
    "pull_request",
    "pull_request_target",
    "workflow_dispatch",
    "repository_dispatch",
    "merge_group",
)


def load_yaml(path: Path):
    with path.open() as fh:
        return yaml.safe_load(fh) or {}


def load_spec(root: Path) -> dict:
    spec = load_yaml(root / ".github" / "green-criteria.yml")
    if spec.get("version") != 1:
        raise ValueError("green-criteria.yml version must be 1")
    return spec


def workflow_on(workflow: dict):
    """The `on:` value. YAML parses the bare word ``on`` as the boolean True."""
    for key in (True, "on"):
        if key in workflow:
            return workflow[key]
    return None


def trigger_names(on):
    if isinstance(on, dict):
        return list(on.keys())
    if isinstance(on, list):
        return [t for t in on if isinstance(t, str)]
    return []


def active_triggers(on) -> list:
    return [t for t in trigger_names(on) if t in ACTIVE_TRIGGERS]


def get_schedule(wf: dict):
    on = workflow_on(wf)
    if isinstance(on, dict):
        sched = on.get("schedule")
        if isinstance(sched, list):
            return sched
    return None


def parse_cron_field(field: str, lo: int, hi: int) -> set:
    values: set = set()
    for part in field.split(","):
        if part == "*":
            values.update(range(lo, hi + 1))
        elif part.startswith("*/"):
            values.update(range(lo, hi + 1, int(part[2:])))
        elif "-" in part:
            a, b = part.split("-", 1)
            values.update(range(int(a), int(b) + 1))
        else:
            values.add(int(part))
    return values


def cron_matches_day(cron: tuple, date: dt.date) -> bool:
    minute, hour, dom, month, dow = cron
    if date.month not in parse_cron_field(month, 1, 12):
        return False
    dom_restricted = dom != "*"
    dow_restricted = dow != "*"
    dom_ok = date.day in parse_cron_field(dom, 1, 31)
    cron_dow = (date.weekday() + 1) % 7  # Python Mon=0..Sun=6 -> cron Sun=0..Sat=6
    dow_ok = cron_dow in parse_cron_field(dow, 0, 6)
    if dom_restricted and dow_restricted:
        if not (dom_ok or dow_ok):
            return False
    elif dom_restricted and not dom_ok:
        return False
    elif dow_restricted and not dow_ok:
        return False
    return True


def max_schedule_gap_days(schedule) -> int | None:
    """Largest gap in days between consecutive scheduled run days.

    The cron entries are matched against every day of a 400-day window that
    starts on a Monday and spans a leap year, so it captures the true worst
    case for monthly and weekly crons. Returns None when no cron parses or the
    schedule fires too rarely to measure.
    """
    crons = []
    for entry in schedule or []:
        parts = (entry or {}).get("cron", "").split()
        if len(parts) == 5:
            crons.append(tuple(parts))
    if not crons:
        return None
    start = dt.date(2024, 1, 1)  # a Monday; 2024 is a leap year
    matching = [
        d
        for d in (start + dt.timedelta(days=i) for i in range(400))
        if any(cron_matches_day(c, d) for c in crons)
    ]
    if len(matching) < 2:
        return None
    return max((matching[i] - matching[i - 1]).days for i in range(1, len(matching)))


def promote_needs(root: Path, promote_job: str) -> set:
    wf = load_yaml(root / ".github" / "workflows" / "post-testing-e2e.yml")
    needs = wf.get("jobs", {}).get(promote_job, {}).get("needs", [])
    return set(needs) if isinstance(needs, list) else set()


def promote_if(root: Path, promote_job: str) -> str | None:
    """The promote job's ``if:`` condition, or None if the job/condition is
    absent. A promote job that runs unconditionally (no ``if:`` or
    ``if: always()``) lets promotion proceed even after a gated job fails."""
    wf = load_yaml(root / ".github" / "workflows" / "post-testing-e2e.yml")
    job = wf.get("jobs", {}).get(promote_job)
    if not job:
        return None
    return job.get("if")


def check_spec(spec: dict, root: Path, promote_job: str = PROMOTE_JOB_DEFAULT) -> list:
    """Return a list of human-readable contract violations (empty = green)."""
    errors: list = []
    needs = promote_needs(root, promote_job)
    promote_condition = promote_if(root, promote_job)

    for crit in spec.get("criteria", []):
        cid = crit.get("id", "<unnamed>")
        enforcement = crit.get("enforcement")
        sla = crit.get("freshness_sla_days")
        for gate in crit.get("gates", []):
            wf_rel = gate.get("workflow")
            job = gate.get("job")
            script = gate.get("script")
            if not wf_rel or not job:
                errors.append(f"{cid}: gate is missing workflow or job")
                continue

            wf_path = root / wf_rel
            if not wf_path.is_file():
                errors.append(f"{cid}: workflow not found: {wf_rel}")
                continue
            wf = load_yaml(wf_path)
            jobs = wf.get("jobs", {})
            if job not in jobs:
                errors.append(f"{cid}: job {job!r} not in {wf_rel}")
                continue
            job_def = jobs[job]

            if script and not (root / script).is_file():
                errors.append(f"{cid}: script not found: {script}")

            if not active_triggers(workflow_on(wf)):
                errors.append(f"{cid}: {wf_rel} is not reachable from any trigger")

            if enforcement != "blocking":
                continue

            if job not in needs:
                errors.append(
                    f"{cid}: blocking gate {job!r} is not in "
                    f"{promote_job}'s needs {sorted(needs)}"
                )
            if job_def.get("continue-on-error"):
                errors.append(
                    f"{cid}: blocking gate {job!r} sets continue-on-error and "
                    f"can fail silently"
                )

            # A promote job that runs unconditionally (no `if:`) or that runs on
            # always()/failure() lets promotion proceed after this gate fails,
            # so a dead or failing gate would still graduate. A bare
            # `!cancelled()` is handled by the requires-success check below (a
            # `!cancelled()` that does not also require the gate to succeed is
            # caught there); the shipped promote job uses `!cancelled()`
            # alongside the gate-success requirement, so flagging `!cancelled()`
            # here would be a false positive. See
            # .github/green-criteria.yml rule.skipped_is_not_green.
            if not promote_condition:
                errors.append(
                    f"{cid}: blocking gate {job!r} is not required to succeed "
                    f"in {promote_job}'s if: (promote runs unconditionally)"
                )
            elif any(tok in promote_condition for tok in ("always()", "failure()")):
                errors.append(
                    f"{cid}: {promote_job} if: {promote_condition!r} lets "
                    f"promotion proceed when {job!r} fails"
                )
            if f"needs.{job}.result == 'success'" not in (promote_condition or ""):
                errors.append(
                    f"{cid}: blocking gate {job!r} is not required to succeed "
                    f"in {promote_job}'s if:"
                )

            schedule = get_schedule(wf)
            if sla is not None and schedule is not None:
                gap = max_schedule_gap_days(schedule)
                if gap is not None and gap > sla:
                    errors.append(
                        f"{cid}: {wf_rel} fires every {gap}d but "
                        f"freshness_sla_days={sla}"
                    )
    return errors


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=".", help="repository root (default: .)")
    parser.add_argument(
        "--promote-job",
        default=PROMOTE_JOB_DEFAULT,
        help="promotion job name to read `needs` from (default: %(default)s)",
    )
    args = parser.parse_args(argv)

    root = Path(args.repo).resolve()
    try:
        spec = load_spec(root)
    except FileNotFoundError:
        print(f"ERROR: {root / '.github' / 'green-criteria.yml'} not found",
              file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    errors = check_spec(spec, root, args.promote_job)
    if errors:
        print(f"green contract violated ({len(errors)} issue(s)):")
        for err in errors:
            print(f"  - {err}")
        return 1
    print("green contract satisfied")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
