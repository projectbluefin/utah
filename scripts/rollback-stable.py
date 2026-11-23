#!/usr/bin/env python3
"""Prepare a read-only, signature- and E2E-verified stable rollback plan."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

spec = importlib.util.spec_from_file_location(
    "promotion_ratchet", Path(__file__).with_name("promotion-ratchet.py")
)
ratchet = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ratchet)

# Same signer policy as execute-release.yml; do not accept arbitrary workflows
# or branch names. Utah publication delegates signing to this reusable workflow.
IDENTITY = r"^https://github\.com/projectbluefin/actions/\.github/workflows/reusable-build\.yml@[0-9a-f]{40}$"
ISSUER = "https://token.actions.githubusercontent.com"


def prepare(target, flavors, registry, github, runner=subprocess.run):
    if not (ratchet.VERSION_RE.fullmatch(target) or ratchet.DIGEST_RE.fullmatch(target)):
        raise ratchet.PromotionError("target must be testing-<yyyymmdd>-<sha> or sha256:<64 hex>")
    plan = []
    for flavor in flavors:
        image = flavor["image"]
        base = f"ghcr.io/projectbluefin/{image}"
        ref = f"{base}@{target}" if ratchet.DIGEST_RE.fullmatch(target) else f"{base}:{target}"
        digest = registry.digest(ref)
        if digest is None:
            raise ratchet.PromotionError(f"{ref}: target does not exist")
        # Resolve once, then verify, read labels, look up evidence and copy only
        # by this digest. A same-day tag rebuild cannot change the chosen bytes.
        pinned = f"{base}@{digest}"
        result = runner([
            "cosign", "verify", "--certificate-identity-regexp", IDENTITY,
            "--certificate-oidc-issuer", ISSUER, pinned,
        ], capture_output=True, text=True, check=False)
        if result.returncode:
            raise ratchet.PromotionError(f"{pinned}: signature verification failed: {result.stderr}")
        commit = ratchet.require_status(image, digest, pinned, registry, github)
        previous = registry.digest(f"{base}:stable")
        if previous is None:
            raise ratchet.PromotionError(f"{base}:stable does not exist; refusing a rollback")
        plan.append({"image": image, "from": previous, "to": digest, "sha": commit.sha})
    if not plan:
        raise ratchet.PromotionError("no active flavors")
    return plan


def main():
    try:
        reason = os.environ["REASON"]
        if not reason.strip():
            raise ratchet.PromotionError("a non-empty reason is required")
        flavors = json.loads(Path("config/flavors.json").read_text())["flavors"]
        plan = prepare(os.environ["TARGET"], flavors, ratchet.Registry(),
                       ratchet.GitHub(os.environ["GITHUB_REPOSITORY"]))
        notes = ["# Stable rollback plan", "", f"Actor: @{os.environ['GITHUB_ACTOR']}",
                 f"Run: https://github.com/{os.environ['GITHUB_REPOSITORY']}/actions/runs/{os.environ['GITHUB_RUN_ID']}",
                 f"Target: {os.environ['TARGET']}", f"Dry run: {os.environ.get('DRY_RUN', 'true')}",
                 "", "## Reason", "", reason, "",
                 "## Verified digest changes", "", "| Image | From | To | Build commit |",
                 "| --- | --- | --- | --- |"]
        for entry in plan:
            line = f"{entry['image']}: {entry['from']} → {entry['to']}"
            print(line)
            notes.append(f"| {entry['image']} | `{entry['from']}` | `{entry['to']}` | `{entry['sha']}` |")
        notes.extend(["", "All targets passed cosign verification and both per-digest E2E gates."])
        Path("rollback-notes.md").write_text("\n".join(notes) + "\n")
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as summary:
            summary.write("\n".join(notes) + "\n")
        with open(os.environ["GITHUB_OUTPUT"], "a") as output:
            output.write(f"plan={json.dumps(plan, separators=(',', ':'))}\n")
        return 0
    except (ratchet.PromotionError, KeyError, ValueError) as exc:
        print(f"rollback refused: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
