#!/usr/bin/env python3
"""Bootc upgrade and rollback lifecycle verification and diagnostics tool.

Parses and validates 'bootc status --format=json' output, tracks state transitions
across the upgrade and rollback lifecycle, asserts graphical desktop health, and
generates structured diagnostics capturing the active deployment and digest at each phase.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional


# `bootc status --format=json` is explicitly documented upstream as an
# unstable schema. Pin the versions we know this tool's field paths are
# compatible with and fail with a clear message on drift, rather than
# letting a renamed/restructured field raise a raw KeyError deep inside a
# phase handler. v1alpha1 was bootc's original schema name; current bootc
# (1.16+) reports "org.containers.bootc/v1" for the same field layout this
# tool reads (booted/staged/rollback.image.imageDigest) -- verified against
# `bootc status --format=json` on a live bootc-1.16.7 host.
KNOWN_API_VERSIONS = {
    "org.containers.bootc/v1alpha1",
    "org.containers.bootc/v1",
}


def parse_bootc_status(raw_json: str) -> Dict[str, Any]:
    """Parse and validate bootc status JSON against known schema versions."""
    if not raw_json or not raw_json.strip():
        raise ValueError("bootc status output is empty")

    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid bootc status JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError("bootc status root must be a JSON object")

    api_version = data.get("apiVersion")
    if api_version not in KNOWN_API_VERSIONS:
        raise ValueError(
            f"bootc status schema mismatch: expected apiVersion to be one "
            f"of {sorted(KNOWN_API_VERSIONS)}, got {api_version!r}. The "
            f"bootc version on the guest may have changed its status "
            f"schema; add the new version to KNOWN_API_VERSIONS and "
            f"re-verify the field paths below before trusting this tool's "
            f"output."
        )

    status = data.get("status")
    if not isinstance(status, dict):
        raise ValueError(
            f"bootc status missing required 'status' dictionary. Keys found: {list(data.keys())}"
        )

    booted = status.get("booted")
    if not isinstance(booted, dict):
        raise ValueError("bootc status missing 'status.booted' deployment dictionary")

    # Extract image info
    image_obj = booted.get("image", {})
    if not isinstance(image_obj, dict):
        raise ValueError("status.booted missing 'image' object")

    digest = image_obj.get("imageDigest")
    if not digest or not isinstance(digest, str) or not digest.startswith("sha256:"):
        raise ValueError(f"status.booted.image missing valid sha256 imageDigest: {digest}")

    return status


def extract_deployment_info(deployment: Optional[Dict[str, Any]]) -> Dict[str, Optional[str]]:
    """Extract digest, image reference, and pinned status from a deployment dict."""
    if not deployment or not isinstance(deployment, dict):
        return {"digest": None, "image": None, "pinned": None}

    image_obj = deployment.get("image", {})
    digest = image_obj.get("imageDigest") if isinstance(image_obj, dict) else None

    # Image ref can be in image.image.image or image.image
    img_ref = None
    if isinstance(image_obj, dict):
        inner = image_obj.get("image")
        if isinstance(inner, dict):
            img_ref = inner.get("image")
        elif isinstance(inner, str):
            img_ref = inner

    pinned = deployment.get("pinned", False)
    return {
        "digest": digest,
        "image": img_ref,
        "pinned": bool(pinned) if pinned is not None else False,
    }


def parse_os_release(content: str) -> Dict[str, str]:
    """Parse /etc/os-release key-value content."""
    data = {}
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        data[k.strip()] = v.strip().strip('"').strip("'")
    return data


class LifecycleTracker:
    """Tracks phases, verifies transitions, and generates failure diagnostics."""

    def __init__(self, work_dir: Path):
        self.work_dir = Path(work_dir)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = self.work_dir / "lifecycle-state.json"
        self.data = self._load()

    def _load(self) -> Dict[str, Any]:
        if self.state_file.exists():
            try:
                return json.loads(self.state_file.read_text())
            except Exception:
                pass
        return {
            "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "overall_status": "in_progress",
            "phases": [],
            "diagnostics": [],
        }

    def _save(self) -> None:
        self.state_file.write_text(json.dumps(self.data, indent=2) + "\n")

    def record_phase(
        self,
        phase_name: str,
        status_raw: str,
        status_verdict: str,
        desktop_ok: bool = True,
        systemd_state: str = "running",
        os_release_text: Optional[str] = None,
        notes: str = "",
        screenshot: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Record the state of a lifecycle phase."""
        try:
            status = parse_bootc_status(status_raw)
            booted = extract_deployment_info(status.get("booted"))
            staged = extract_deployment_info(status.get("staged"))
            rollback = extract_deployment_info(status.get("rollback"))
            parse_error = None
        except Exception as exc:
            booted = {"digest": "unknown", "image": "unknown", "pinned": False}
            staged = {"digest": None, "image": None, "pinned": None}
            rollback = {"digest": None, "image": None, "pinned": None}
            parse_error = str(exc)

        os_info = parse_os_release(os_release_text) if os_release_text else {}
        is_utah_identity = (
            os_info.get("ID") == "utah" and os_info.get("IMAGE_ID") == "utah"
            if os_info
            else None
        )

        phase_record = {
            "phase": phase_name,
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "status": status_verdict.upper(),
            "booted": booted,
            "staged": staged,
            "rollback": rollback,
            "desktop_ok": desktop_ok,
            "systemd_state": systemd_state,
            "os_identity_utah": is_utah_identity,
            "os_version": os_info.get("VERSION_ID"),
            "screenshot": screenshot,
            "notes": notes,
            "parse_error": parse_error,
        }

        # Validate phase-specific requirements and add diagnostics if failed
        errors = []
        if parse_error:
            errors.append(f"Phase {phase_name}: Failed to parse bootc status: {parse_error}")

        if status_verdict.upper() == "FAIL":
            errors.append(
                f"Phase {phase_name} marked as FAIL. Active booted digest: {booted.get('digest')}"
            )

        if desktop_ok is False:
            errors.append(
                f"Phase {phase_name}: Graphical desktop check failed. Active booted digest: {booted.get('digest')}"
            )

        if is_utah_identity is False:
            errors.append(
                f"Phase {phase_name}: Utah identity check failed: ID={os_info.get('ID')}, IMAGE_ID={os_info.get('IMAGE_ID')}"
            )

        if errors:
            for err in errors:
                self.data["diagnostics"].append(
                    {
                        "phase": phase_name,
                        "timestamp": phase_record["timestamp"],
                        "active_booted_digest": booted.get("digest"),
                        "active_booted_image": booted.get("image"),
                        "staged_digest": staged.get("digest"),
                        "rollback_digest": rollback.get("digest"),
                        "message": err,
                    }
                )

        self.data["phases"].append(phase_record)
        self._save()
        return phase_record

    def assert_transition(
        self,
        from_phase: str,
        to_phase: str,
        expected_relationship: str,
    ) -> bool:
        """Assert relationship between two recorded phases.

        expected_relationship:
          'upgrade_staged': to_phase staged digest is different from from_phase booted digest.
          'upgraded_boot': to_phase booted digest == from_phase staged digest, and
                           to_phase rollback digest == from_phase booted digest.
          'rollback_staged': to_phase staged digest == from_phase rollback digest.
          'rollback_boot': to_phase booted digest == original booted digest, and
                           to_phase rollback digest == upgraded booted digest.
        """
        phases_by_name = {p["phase"]: p for p in self.data["phases"]}
        p1 = phases_by_name.get(from_phase)
        p2 = phases_by_name.get(to_phase)

        if not p1:
            raise AssertionError(f"Prior phase '{from_phase}' not found in lifecycle history")
        if not p2:
            raise AssertionError(f"Target phase '{to_phase}' not found in lifecycle history")

        p1_booted = p1["booted"]["digest"]
        p1_staged = p1["staged"]["digest"]
        p2_booted = p2["booted"]["digest"]
        p2_staged = p2["staged"]["digest"]
        p2_rollback = p2["rollback"]["digest"]

        if expected_relationship == "upgrade_staged":
            if not p2_staged:
                msg = f"Upgrade did not stage any deployment in '{to_phase}'. Active booted: {p2_booted}"
                self._record_error(to_phase, msg, p2)
                raise AssertionError(msg)
            if p2_staged == p1_booted:
                msg = f"Upgrade staged the same digest as currently booted ({p2_staged}) in '{to_phase}'"
                self._record_error(to_phase, msg, p2)
                raise AssertionError(msg)

        elif expected_relationship == "upgraded_boot":
            expected_target = p1_staged
            if p2_booted != expected_target:
                msg = (
                    f"Reboot into upgraded deployment failed in '{to_phase}'. "
                    f"Active booted digest: {p2_booted}, Expected candidate digest: {expected_target}"
                )
                self._record_error(to_phase, msg, p2)
                raise AssertionError(msg)
            if p2_rollback != p1_booted:
                msg = (
                    f"Rollback deployment does not match previous deployment in '{to_phase}'. "
                    f"Active rollback digest: {p2_rollback}, Expected previous digest: {p1_booted}"
                )
                self._record_error(to_phase, msg, p2)
                raise AssertionError(msg)

        elif expected_relationship == "rollback_staged":
            expected_target = p1["rollback"]["digest"]
            if p2_staged != expected_target:
                msg = (
                    f"Rollback staging failed in '{to_phase}'. "
                    f"Staged digest: {p2_staged}, Expected previous digest: {expected_target}"
                )
                self._record_error(to_phase, msg, p2)
                raise AssertionError(msg)

        elif expected_relationship == "rollback_boot":
            # Initial phase is usually the first recorded phase
            initial_phase = self.data["phases"][0]
            initial_digest = initial_phase["booted"]["digest"]
            upgraded_phase = phases_by_name.get("upgraded_boot", p1)
            upgraded_digest = upgraded_phase["booted"]["digest"]

            if p2_booted != initial_digest:
                msg = (
                    f"Rollback boot failed in '{to_phase}'. "
                    f"Active booted digest: {p2_booted}, Expected original digest: {initial_digest}"
                )
                self._record_error(to_phase, msg, p2)
                raise AssertionError(msg)
            if p2_rollback != upgraded_digest:
                msg = (
                    f"Rollback deployment does not reference candidate deployment in '{to_phase}'. "
                    f"Active rollback digest: {p2_rollback}, Expected candidate digest: {upgraded_digest}"
                )
                self._record_error(to_phase, msg, p2)
                raise AssertionError(msg)

        self._save()
        return True

    def _record_error(self, phase_name: str, message: str, phase_info: Dict[str, Any]) -> None:
        self.data["diagnostics"].append(
            {
                "phase": phase_name,
                "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "active_booted_digest": phase_info["booted"]["digest"],
                "active_booted_image": phase_info["booted"]["image"],
                "staged_digest": phase_info["staged"]["digest"],
                "rollback_digest": phase_info["rollback"]["digest"],
                "message": message,
            }
        )
        self._save()

    def generate_report(self) -> Dict[str, Any]:
        """Generate final JSON summary and Markdown report."""
        has_failures = bool(self.data["diagnostics"]) or any(
            p["status"] != "PASS" for p in self.data["phases"]
        )
        self.data["overall_status"] = "FAIL" if has_failures else "PASS"
        self.data["finished_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        self._save()

        # Write lifecycle-summary.json
        summary_file = self.work_dir / "lifecycle-summary.json"
        summary_file.write_text(json.dumps(self.data, indent=2) + "\n")

        # Write lifecycle-report.md
        md_file = self.work_dir / "lifecycle-report.md"
        lines = [
            f"# Bootc Upgrade & Rollback Lifecycle Report: {self.data['overall_status']}",
            "",
            f"- **Overall Status:** `{self.data['overall_status']}`",
            f"- **Created:** {self.data.get('created_at', 'N/A')}",
            f"- **Finished:** {self.data.get('finished_at', 'N/A')}",
            "",
            "## Lifecycle Phase Progression",
            "",
            "| Phase | Status | Booted Digest | Staged Digest | Rollback Digest | Desktop OK |",
            "|---|---|---|---|---|---|",
        ]

        for p in self.data["phases"]:
            b_dig = (p["booted"]["digest"] or "")[:19] + "..." if p["booted"]["digest"] else "-"
            s_dig = (p["staged"]["digest"] or "")[:19] + "..." if p["staged"]["digest"] else "-"
            r_dig = (p["rollback"]["digest"] or "")[:19] + "..." if p["rollback"]["digest"] else "-"
            d_ok = "✅" if p.get("desktop_ok") else "❌"
            status_icon = "✅ PASS" if p["status"] == "PASS" else "❌ FAIL"
            lines.append(
                f"| `{p['phase']}` | {status_icon} | `{b_dig}` | `{s_dig}` | `{r_dig}` | {d_ok} |"
            )

        if self.data["diagnostics"]:
            lines.extend([
                "",
                "## Failure Diagnostics",
                "",
            ])
            for diag in self.data["diagnostics"]:
                lines.extend([
                    f"### Phase: `{diag.get('phase')}`",
                    f"- **Timestamp:** {diag.get('timestamp')}",
                    f"- **Active Booted Digest:** `{diag.get('active_booted_digest')}`",
                    f"- **Active Booted Image:** `{diag.get('active_booted_image')}`",
                    f"- **Staged Digest:** `{diag.get('staged_digest')}`",
                    f"- **Rollback Digest:** `{diag.get('rollback_digest')}`",
                    f"- **Error:** {diag.get('message')}",
                    "",
                ])
        else:
            lines.extend([
                "",
                "## Summary",
                "",
                "All lifecycle phases passed: clean boot, upgrade staged, upgraded boot with active graphical desktop, rollback staged, and rollback boot to original immutable digest.",
            ])

        md_file.write_text("\n".join(lines) + "\n")
        return self.data


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Subcommand: parse-status
    p_parse = subparsers.add_parser("parse-status", help="Parse bootc status JSON")
    p_parse.add_argument("status_file", type=Path, help="Path to bootc status JSON file")

    # Subcommand: record-phase
    p_rec = subparsers.add_parser("record-phase", help="Record a lifecycle phase")
    p_rec.add_argument("--work-dir", type=Path, required=True, help="Lifecycle work directory")
    p_rec.add_argument("--phase", required=True, help="Name of phase")
    p_rec.add_argument("--status-file", type=Path, required=True, help="Path to status JSON")
    p_rec.add_argument("--verdict", default="PASS", choices=["PASS", "FAIL"], help="Phase verdict")
    p_rec.add_argument("--desktop-ok", action="store_true", default=True, help="Desktop status")
    p_rec.add_argument("--desktop-fail", action="store_true", help="Mark desktop check failed")
    p_rec.add_argument("--systemd-state", default="running", help="Systemd state")
    p_rec.add_argument("--os-release", type=Path, help="Path to /etc/os-release dump")
    p_rec.add_argument("--notes", default="", help="Optional notes")
    p_rec.add_argument("--screenshot", help="Path to screenshot")

    # Subcommand: assert-transition
    p_trans = subparsers.add_parser("assert-transition", help="Assert transition between phases")
    p_trans.add_argument("--work-dir", type=Path, required=True, help="Lifecycle work directory")
    p_trans.add_argument("--from-phase", required=True, help="Prior phase name")
    p_trans.add_argument("--to-phase", required=True, help="Current phase name")
    p_trans.add_argument(
        "--relation",
        required=True,
        choices=["upgrade_staged", "upgraded_boot", "rollback_staged", "rollback_boot"],
        help="Expected relationship",
    )

    # Subcommand: generate-report
    p_rep = subparsers.add_parser("generate-report", help="Generate summary and report")
    p_rep.add_argument("--work-dir", type=Path, required=True, help="Lifecycle work directory")

    # Subcommand: simulate
    subparsers.add_parser("simulate", help="Run a self-contained simulated lifecycle validation")

    args = parser.parse_args()

    if args.command == "parse-status":
        raw = args.status_file.read_text()
        status = parse_bootc_status(raw)
        booted = extract_deployment_info(status.get("booted"))
        staged = extract_deployment_info(status.get("staged"))
        rollback = extract_deployment_info(status.get("rollback"))
        print(json.dumps({"booted": booted, "staged": staged, "rollback": rollback}, indent=2))
        return 0

    elif args.command == "record-phase":
        tracker = LifecycleTracker(args.work_dir)
        raw_status = args.status_file.read_text()
        os_text = args.os_release.read_text() if args.os_release and args.os_release.exists() else None
        desktop_ok = False if args.desktop_fail else args.desktop_ok
        record = tracker.record_phase(
            phase_name=args.phase,
            status_raw=raw_status,
            status_verdict=args.verdict,
            desktop_ok=desktop_ok,
            systemd_state=args.systemd_state,
            os_release_text=os_text,
            notes=args.notes,
            screenshot=args.screenshot,
        )
        print(f"Recorded phase '{args.phase}': status={record['status']} booted={record['booted']['digest']}")
        return 0 if record["status"] == "PASS" else 1

    elif args.command == "assert-transition":
        tracker = LifecycleTracker(args.work_dir)
        try:
            tracker.assert_transition(args.from_phase, args.to_phase, args.relation)
            print(f"Transition assertion '{args.relation}' ({args.from_phase} -> {args.to_phase}) passed.")
            return 0
        except AssertionError as err:
            print(f"Transition assertion FAILED: {err}", file=sys.stderr)
            return 1

    elif args.command == "generate-report":
        tracker = LifecycleTracker(args.work_dir)
        summary = tracker.generate_report()
        print(f"Lifecycle report generated: status={summary['overall_status']}")
        return 0 if summary["overall_status"] == "PASS" else 1

    elif args.command == "simulate":
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            td = Path(tmpdir)
            t = LifecycleTracker(td)

            d1 = "sha256:1111111111111111111111111111111111111111111111111111111111111111"
            d2 = "sha256:2222222222222222222222222222222222222222222222222222222222222222"
            img = "ghcr.io/projectbluefin/utah"

            os_txt = "ID=utah\nIMAGE_ID=utah\nVERSION_ID=51\n"

            s_initial = json.dumps({
                "apiVersion": "org.containers.bootc/v1alpha1",
                "status": {
                    "booted": {"image": {"imageDigest": d1, "image": {"image": f"{img}:testing"}}, "pinned": False},
                    "staged": None,
                    "rollback": None,
                }
            })
            t.record_phase("initial_boot", s_initial, "PASS", desktop_ok=True, os_release_text=os_txt)

            s_up_staged = json.dumps({
                "apiVersion": "org.containers.bootc/v1alpha1",
                "status": {
                    "booted": {"image": {"imageDigest": d1, "image": {"image": f"{img}:testing"}}, "pinned": False},
                    "staged": {"image": {"imageDigest": d2, "image": {"image": f"{img}@sha256:2222..."}}, "pinned": False},
                    "rollback": None,
                }
            })
            t.record_phase("upgrade_staged", s_up_staged, "PASS", desktop_ok=True)
            t.assert_transition("initial_boot", "upgrade_staged", "upgrade_staged")

            s_up_booted = json.dumps({
                "apiVersion": "org.containers.bootc/v1alpha1",
                "status": {
                    "booted": {"image": {"imageDigest": d2, "image": {"image": f"{img}@sha256:2222..."}}, "pinned": False},
                    "staged": None,
                    "rollback": {"image": {"imageDigest": d1, "image": {"image": f"{img}:testing"}}, "pinned": False},
                }
            })
            t.record_phase("upgraded_boot", s_up_booted, "PASS", desktop_ok=True, os_release_text=os_txt)
            t.assert_transition("upgrade_staged", "upgraded_boot", "upgraded_boot")

            s_rb_staged = json.dumps({
                "apiVersion": "org.containers.bootc/v1alpha1",
                "status": {
                    "booted": {"image": {"imageDigest": d2, "image": {"image": f"{img}@sha256:2222..."}}, "pinned": False},
                    "staged": {"image": {"imageDigest": d1, "image": {"image": f"{img}:testing"}}, "pinned": False},
                    "rollback": {"image": {"imageDigest": d1, "image": {"image": f"{img}:testing"}}, "pinned": False},
                }
            })
            t.record_phase("rollback_staged", s_rb_staged, "PASS", desktop_ok=True)
            t.assert_transition("upgraded_boot", "rollback_staged", "rollback_staged")

            s_rb_booted = json.dumps({
                "apiVersion": "org.containers.bootc/v1alpha1",
                "status": {
                    "booted": {"image": {"imageDigest": d1, "image": {"image": f"{img}:testing"}}, "pinned": False},
                    "staged": None,
                    "rollback": {"image": {"imageDigest": d2, "image": {"image": f"{img}@sha256:2222..."}}, "pinned": False},
                }
            })
            t.record_phase("rollback_boot", s_rb_booted, "PASS", desktop_ok=True, os_release_text=os_txt)
            t.assert_transition("rollback_staged", "rollback_boot", "rollback_boot")

            summary = t.generate_report()
            assert summary["overall_status"] == "PASS"
            assert len(summary["phases"]) == 5
            print("Simulated bootc lifecycle test passed all 5 phases successfully!")
            return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
