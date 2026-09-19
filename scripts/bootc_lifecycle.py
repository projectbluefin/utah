#!/usr/bin/env python3
"""Bootc upgrade and rollback lifecycle validator and diagnostics helper.

Parses `bootc status --format=json` output, validates lifecycle transitions
across baseline, staged, upgraded, and rollback phases, and formats structured
failure diagnostics identifying the active deployment and digest at each phase.
"""

from __future__ import annotations

import argparse
import datetime
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass
class DeploymentInfo:
    slot: str
    image: str
    transport: str
    digest: str
    version: str | None = None
    timestamp: str | None = None
    pinned: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _extract_slot_deployment(slot_name: str, entry: dict[str, Any] | None) -> DeploymentInfo | None:
    if not entry or not isinstance(entry, dict):
        return None

    # Handle image container info: entry may contain "image" dictionary
    img_data = entry.get("image")
    if not isinstance(img_data, dict):
        return None

    # Extract image reference and transport
    ref_obj = img_data.get("image")
    if isinstance(ref_obj, dict):
        image_name = ref_obj.get("image", "")
        transport = ref_obj.get("transport", "")
    elif isinstance(ref_obj, str):
        image_name = ref_obj
        transport = img_data.get("transport", "")
    else:
        image_name = ""
        transport = ""

    # Extract image digest: handle imageDigest, image_digest, digest
    digest = (
        img_data.get("imageDigest")
        or img_data.get("image_digest")
        or img_data.get("digest")
        or entry.get("imageDigest")
        or entry.get("image_digest")
        or ""
    )

    version = img_data.get("version") or entry.get("version")
    timestamp = img_data.get("timestamp") or entry.get("timestamp")
    pinned = bool(entry.get("pinned", False))

    return DeploymentInfo(
        slot=slot_name,
        image=str(image_name),
        transport=str(transport),
        digest=str(digest),
        version=str(version) if version is not None else None,
        timestamp=str(timestamp) if timestamp is not None else None,
        pinned=pinned,
    )


def parse_bootc_status(raw_data: str | dict[str, Any]) -> dict[str, DeploymentInfo]:
    """Parse raw JSON string or dict from `bootc status --format=json`."""
    if isinstance(raw_data, str):
        try:
            data = json.loads(raw_data)
        except Exception as exc:
            raise ValueError(f"Invalid bootc status JSON: {exc}") from exc
    elif isinstance(raw_data, dict):
        data = raw_data
    else:
        raise ValueError(f"Expected str or dict, got {type(raw_data).__name__}")

    # The JSON schema wraps HostStatus either under top-level 'status' or at root
    host_status = data.get("status", data)
    if not isinstance(host_status, dict):
        raise ValueError("Invalid bootc status structure: status is not an object")

    deployments: dict[str, DeploymentInfo] = {}

    for slot in ("booted", "staged", "rollback"):
        dep = _extract_slot_deployment(slot, host_status.get(slot))
        if dep:
            deployments[slot] = dep

    return deployments


def validate_phase_transition(
    phase: str,
    status_data: str | dict[str, Any],
    baseline_digest: str | None = None,
    candidate_digest: str | None = None,
) -> tuple[bool, str, dict[str, Any]]:
    """Validate lifecycle phase invariants against current bootc status."""
    deployments = parse_bootc_status(status_data)
    booted = deployments.get("booted")
    staged = deployments.get("staged")
    rollback = deployments.get("rollback")

    diag: dict[str, Any] = {
        "phase": phase,
        "booted": booted.to_dict() if booted else None,
        "staged": staged.to_dict() if staged else None,
        "rollback": rollback.to_dict() if rollback else None,
    }

    if phase == "baseline":
        if not booted:
            return False, "No booted deployment found in bootc status", diag
        if not booted.digest:
            return False, "Booted deployment is missing an image digest", diag
        return True, f"Baseline deployment active with digest {booted.digest}", diag

    elif phase == "staged":
        if not booted:
            return False, "No booted deployment active during staging", diag
        if baseline_digest and booted.digest != baseline_digest:
            return (
                False,
                f"Atomic guarantee violated: booted digest changed before reboot (expected {baseline_digest}, got {booted.digest})",
                diag,
            )
        if not staged:
            return False, "No staged deployment found after upgrade command", diag
        if not candidate_digest:
            return False, "Candidate digest is required for staged phase validation", diag
        if staged.digest != candidate_digest:
            return (
                False,
                f"Staged digest '{staged.digest}' does not match candidate digest '{candidate_digest}'",
                diag,
            )
        return True, f"Upgrade staged successfully with digest {staged.digest}", diag

    elif phase == "upgraded":
        if not booted:
            return False, "No booted deployment active after rebooting upgraded image", diag
        if not candidate_digest:
            return False, "Candidate digest is required for upgraded phase validation", diag
        if booted.digest != candidate_digest:
            return (
                False,
                f"Upgraded boot failed: booted digest '{booted.digest}' does not match candidate '{candidate_digest}'",
                diag,
            )
        if not rollback:
            return False, "Rollback deployment missing after upgrade", diag
        if baseline_digest and rollback.digest != baseline_digest:
            return (
                False,
                f"Rollback deployment digest '{rollback.digest}' does not point to baseline '{baseline_digest}'",
                diag,
            )
        return True, f"Upgraded deployment active with digest {booted.digest}", diag

    elif phase == "rollback":
        if not booted:
            return False, "No booted deployment active after rollback reboot", diag
        if baseline_digest and booted.digest != baseline_digest:
            return (
                False,
                f"Rollback verification failed: booted digest '{booted.digest}' does not match baseline '{baseline_digest}'",
                diag,
            )
        return True, f"Rollback successfully restored baseline deployment {booted.digest}", diag

    else:
        return False, f"Unknown lifecycle phase: {phase}", diag


def record_phase_diagnostics(
    output_dir: Path,
    phase: str,
    deployment_type: str,
    digest: str,
    status: str,
    reason: str | None = None,
    details: dict[str, Any] | None = None,
) -> Path:
    """Write structured diagnostics for a phase to output_dir."""
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
    record = {
        "timestamp": timestamp,
        "phase": phase,
        "deployment_type": deployment_type,
        "digest": digest,
        "status": status,
        "reason": reason,
        "details": details or {},
    }

    record_path = output_dir / f"lifecycle-{phase}.json"
    record_path.write_text(json.dumps(record, indent=2) + "\n")
    return record_path


def format_failure_summary(
    phase: str,
    active_deployment: str,
    active_digest: str,
    expected_digest: str | None,
    reason: str,
) -> str:
    """Format human-readable failure diagnostics identifying deployment and digest."""
    lines = [
        "======================================================================",
        "                    BOOTC LIFECYCLE TEST FAILURE                      ",
        "======================================================================",
        f"Phase:             {phase}",
        f"Active Deployment: {active_deployment}",
        f"Active Digest:     {active_digest or '(unknown)'}",
    ]
    if expected_digest:
        lines.append(f"Expected Digest:   {expected_digest}")
    lines.extend([
        f"Failure Reason:    {reason}",
        "======================================================================",
    ])
    return "\n".join(lines)


def generate_lifecycle_summary(evidence_dir: Path) -> dict[str, Any]:
    """Aggregate phase records into a final lifecycle summary."""
    summary: dict[str, Any] = {
        "status": "PASS",
        "phases": {},
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }

    for phase in ("baseline", "staged", "upgraded", "rollback"):
        record_file = evidence_dir / f"lifecycle-{phase}.json"
        if record_file.is_file():
            data = json.loads(record_file.read_text())
            summary["phases"][phase] = data
            if data.get("status") != "PASS":
                summary["status"] = "FAIL"

    summary_file = evidence_dir / "lifecycle-summary.json"
    summary_file.write_text(json.dumps(summary, indent=2) + "\n")
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    # extract-digest
    p_extract = subparsers.add_parser("extract-digest", help="Extract digest for a given slot")
    p_extract.add_argument("--status", required=True, help="Path to status JSON file or '-' for stdin")
    p_extract.add_argument("--slot", default="booted", choices=["booted", "staged", "rollback"])

    # validate-phase
    p_val = subparsers.add_parser("validate-phase", help="Validate invariants for a lifecycle phase")
    p_val.add_argument("phase", choices=["baseline", "staged", "upgraded", "rollback"])
    p_val.add_argument("--status", required=True, help="Path to status JSON file or '-' for stdin")
    p_val.add_argument("--baseline-digest", help="Expected baseline image digest")
    p_val.add_argument("--candidate-digest", help="Expected candidate image digest")

    # record-diagnostics
    p_diag = subparsers.add_parser("record-diagnostics", help="Record structured diagnostics")
    p_diag.add_argument("--output-dir", required=True, type=Path)
    p_diag.add_argument("--phase", required=True)
    p_diag.add_argument("--deployment", required=True)
    p_diag.add_argument("--digest", required=True)
    p_diag.add_argument("--status", required=True, choices=["PASS", "FAIL", "IN_PROGRESS"])
    p_diag.add_argument("--reason")

    # summary
    p_sum = subparsers.add_parser("summary", help="Generate final lifecycle summary")
    p_sum.add_argument("--evidence-dir", required=True, type=Path)

    # failure-report
    p_fail = subparsers.add_parser("failure-report", help="Print formatted failure diagnostics")
    p_fail.add_argument("--phase", required=True)
    p_fail.add_argument("--deployment", required=True)
    p_fail.add_argument("--digest", required=True)
    p_fail.add_argument("--expected-digest")
    p_fail.add_argument("--reason", required=True)

    args = parser.parse_args(argv)

    if args.subcommand == "extract-digest":
        raw = sys.stdin.read() if args.status == "-" else Path(args.status).read_text()
        deployments = parse_bootc_status(raw)
        dep = deployments.get(args.slot)
        if not dep or not dep.digest:
            print(f"No digest found for slot {args.slot}", file=sys.stderr)
            return 1
        print(dep.digest)
        return 0

    elif args.subcommand == "validate-phase":
        raw = sys.stdin.read() if args.status == "-" else Path(args.status).read_text()
        ok, msg, _diag = validate_phase_transition(
            phase=args.phase,
            status_data=raw,
            baseline_digest=args.baseline_digest,
            candidate_digest=args.candidate_digest,
        )
        if ok:
            print(f"PASS: {msg}")
            return 0
        else:
            print(f"FAIL: {msg}", file=sys.stderr)
            return 1

    elif args.subcommand == "record-diagnostics":
        record_phase_diagnostics(
            output_dir=args.output_dir,
            phase=args.phase,
            deployment_type=args.deployment,
            digest=args.digest,
            status=args.status,
            reason=args.reason,
        )
        return 0

    elif args.subcommand == "summary":
        summary = generate_lifecycle_summary(args.evidence_dir)
        print(f"Lifecycle status: {summary.get('status')}")
        return 0 if summary.get("status") == "PASS" else 1

    elif args.subcommand == "failure-report":
        print(
            format_failure_summary(
                phase=args.phase,
                active_deployment=args.deployment,
                active_digest=args.digest,
                expected_digest=args.expected_digest,
                reason=args.reason,
            ),
            file=sys.stderr,
        )
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
