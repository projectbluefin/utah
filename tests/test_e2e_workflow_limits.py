"""post-testing-e2e.yml bounds its network steps and its ISO artifact storage.

A wedged apt mirror or registry must fail as a named step, not hold the job
until its 180-240 minute timeout. Each production ISO is ~5.5 GB, four per run,
about eight runs a day: at 30 days that was 3.4 TB of retained artifacts on
2026-11-20, nearly all superseded by newer runs.
"""
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
JOBS = yaml.safe_load((ROOT / ".github/workflows/post-testing-e2e.yml").read_text())["jobs"]


def is_network_step(step: dict) -> bool:
    run = step.get("run", "")
    return ("actions/checkout@" in step.get("uses", "")
            or "apt-get" in run
            or "podman pull" in run
            or "podman login" in run
            or "skopeo login" in run)


class NetworkStepTimeoutTests(unittest.TestCase):
    def test_every_network_step_has_its_own_timeout(self):
        steps = [(name, step) for name, job in JOBS.items() for step in job.get("steps", [])
                 if is_network_step(step)]
        self.assertGreaterEqual(len(steps), 15)
        for name, step in steps:
            with self.subTest(job=name, step=step.get("name") or step.get("uses")):
                minutes = step.get("timeout-minutes")
                self.assertIsInstance(minutes, int)
                self.assertLess(minutes, JOBS[name].get("timeout-minutes", 360))

    def test_apt_steps_outlast_the_slowest_apt_run_seen(self):
        # Job 135107979619's apt step took 31m20s and succeeded.
        for name, job in JOBS.items():
            for step in job.get("steps", []):
                if "apt-get install -y podman" in step.get("run", ""):
                    with self.subTest(job=name):
                        self.assertGreaterEqual(step["timeout-minutes"], 35)


class RetentionTests(unittest.TestCase):
    def test_production_isos_are_kept_for_days_not_a_month(self):
        upload = next(s for s in JOBS["production-iso"]["steps"] if s.get("name") == "Retain production ISO")
        self.assertLessEqual(upload["with"]["retention-days"], 3)


if __name__ == "__main__":
    unittest.main()
