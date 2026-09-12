#!/usr/bin/env python3
"""Unit tests for the bootc upgrade and rollback lifecycle verifier and diagnostics."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import importlib.util
import sys

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "verify-bootc-lifecycle.py"

_spec = importlib.util.spec_from_file_location("verify_bootc_lifecycle", SCRIPT_PATH)
if _spec is None or _spec.loader is None:
    raise ImportError(f"Cannot load module from {SCRIPT_PATH}")
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

LifecycleTracker = _mod.LifecycleTracker
extract_deployment_info = _mod.extract_deployment_info
parse_bootc_status = _mod.parse_bootc_status
parse_os_release = _mod.parse_os_release


class BootcLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.d1 = "sha256:1111111111111111111111111111111111111111111111111111111111111111"
        self.d2 = "sha256:2222222222222222222222222222222222222222222222222222222222222222"
        self.img = "ghcr.io/projectbluefin/utah"
        self.utah_os_release = "ID=utah\nIMAGE_ID=utah\nVARIANT_ID=utah\nVERSION_ID=51\n"

    def test_parse_valid_bootc_status(self) -> None:
        payload = {
            "apiVersion": "org.containers.bootc/v1alpha1",
            "status": {
                "booted": {
                    "image": {
                        "imageDigest": self.d1,
                        "image": {"image": f"{self.img}:testing"},
                    },
                    "pinned": False,
                },
                "staged": None,
                "rollback": None,
            }
        }
        status = parse_bootc_status(json.dumps(payload))
        self.assertIn("booted", status)

        info = extract_deployment_info(status["booted"])
        self.assertEqual(info["digest"], self.d1)
        self.assertEqual(info["image"], f"{self.img}:testing")
        self.assertFalse(info["pinned"])

    def test_parse_invalid_bootc_status(self) -> None:
        # Empty
        with self.assertRaises(ValueError):
            parse_bootc_status("")

        # Not JSON
        with self.assertRaises(ValueError):
            parse_bootc_status("not json")

        # Missing .status
        with self.assertRaises(ValueError):
            parse_bootc_status(json.dumps({"booted": {}}))

        # Missing .status.booted
        with self.assertRaises(ValueError):
            parse_bootc_status(json.dumps({"status": {}}))

        # Missing sha256 prefix
        with self.assertRaises(ValueError):
            parse_bootc_status(json.dumps({
                "status": {
                    "booted": {
                        "image": {"imageDigest": "invalid_digest"}
                    }
                }
            }))

    def test_parse_bootc_status_accepts_current_v1_schema(self) -> None:
        # Verified live against bootc-1.16.7: `bootc status --format=json`
        # reports apiVersion "org.containers.bootc/v1", not "v1alpha1".
        payload = {
            "apiVersion": "org.containers.bootc/v1",
            "status": {
                "booted": {
                    "image": {
                        "imageDigest": self.d1,
                        "image": {"image": f"{self.img}:testing"},
                    },
                    "pinned": False,
                },
                "staged": None,
                "rollback": None,
            }
        }
        status = parse_bootc_status(json.dumps(payload))
        self.assertIn("booted", status)

    def test_parse_bootc_status_rejects_unknown_schema_version(self) -> None:
        payload = {
            "apiVersion": "org.containers.bootc/v2",
            "status": {
                "booted": {
                    "image": {"imageDigest": self.d1},
                }
            },
        }
        with self.assertRaises(ValueError) as ctx:
            parse_bootc_status(json.dumps(payload))
        self.assertIn("schema mismatch", str(ctx.exception))

    def test_parse_os_release_utah_identity(self) -> None:
        data = parse_os_release(self.utah_os_release)
        self.assertEqual(data.get("ID"), "utah")
        self.assertEqual(data.get("IMAGE_ID"), "utah")
        self.assertEqual(data.get("VERSION_ID"), "51")

    def test_full_successful_lifecycle_progression(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)
            tracker = LifecycleTracker(work_dir)

            # Phase 1: Initial boot
            s1 = json.dumps({
                "apiVersion": "org.containers.bootc/v1alpha1",
                "status": {
                    "booted": {
                        "image": {"imageDigest": self.d1, "image": {"image": f"{self.img}:testing"}},
                        "pinned": False,
                    },
                    "staged": None,
                    "rollback": None,
                }
            })
            p1 = tracker.record_phase(
                "initial_boot", s1, "PASS", desktop_ok=True, os_release_text=self.utah_os_release
            )
            self.assertEqual(p1["status"], "PASS")
            self.assertEqual(p1["booted"]["digest"], self.d1)
            self.assertTrue(p1["os_identity_utah"])

            # Phase 2: Upgrade staged
            s2 = json.dumps({
                "apiVersion": "org.containers.bootc/v1alpha1",
                "status": {
                    "booted": {
                        "image": {"imageDigest": self.d1, "image": {"image": f"{self.img}:testing"}},
                        "pinned": False,
                    },
                    "staged": {
                        "image": {"imageDigest": self.d2, "image": {"image": f"{self.img}@sha256:2222"}},
                        "pinned": False,
                    },
                    "rollback": None,
                }
            })
            p2 = tracker.record_phase("upgrade_staged", s2, "PASS", desktop_ok=True)
            self.assertEqual(p2["staged"]["digest"], self.d2)
            self.assertTrue(tracker.assert_transition("initial_boot", "upgrade_staged", "upgrade_staged"))

            # Phase 3: Upgraded boot
            s3 = json.dumps({
                "apiVersion": "org.containers.bootc/v1alpha1",
                "status": {
                    "booted": {
                        "image": {"imageDigest": self.d2, "image": {"image": f"{self.img}@sha256:2222"}},
                        "pinned": False,
                    },
                    "staged": None,
                    "rollback": {
                        "image": {"imageDigest": self.d1, "image": {"image": f"{self.img}:testing"}},
                        "pinned": False,
                    },
                }
            })
            p3 = tracker.record_phase(
                "upgraded_boot", s3, "PASS", desktop_ok=True, os_release_text=self.utah_os_release
            )
            self.assertEqual(p3["booted"]["digest"], self.d2)
            self.assertEqual(p3["rollback"]["digest"], self.d1)
            self.assertTrue(tracker.assert_transition("upgrade_staged", "upgraded_boot", "upgraded_boot"))

            # Phase 4: Rollback staged
            s4 = json.dumps({
                "apiVersion": "org.containers.bootc/v1alpha1",
                "status": {
                    "booted": {
                        "image": {"imageDigest": self.d2, "image": {"image": f"{self.img}@sha256:2222"}},
                        "pinned": False,
                    },
                    "staged": {
                        "image": {"imageDigest": self.d1, "image": {"image": f"{self.img}:testing"}},
                        "pinned": False,
                    },
                    "rollback": {
                        "image": {"imageDigest": self.d1, "image": {"image": f"{self.img}:testing"}},
                        "pinned": False,
                    },
                }
            })
            p4 = tracker.record_phase("rollback_staged", s4, "PASS", desktop_ok=True)
            self.assertEqual(p4["staged"]["digest"], self.d1)
            self.assertTrue(tracker.assert_transition("upgraded_boot", "rollback_staged", "rollback_staged"))

            # Phase 5: Rollback boot
            s5 = json.dumps({
                "apiVersion": "org.containers.bootc/v1alpha1",
                "status": {
                    "booted": {
                        "image": {"imageDigest": self.d1, "image": {"image": f"{self.img}:testing"}},
                        "pinned": False,
                    },
                    "staged": None,
                    "rollback": {
                        "image": {"imageDigest": self.d2, "image": {"image": f"{self.img}@sha256:2222"}},
                        "pinned": False,
                    },
                }
            })
            p5 = tracker.record_phase(
                "rollback_boot", s5, "PASS", desktop_ok=True, os_release_text=self.utah_os_release
            )
            self.assertEqual(p5["booted"]["digest"], self.d1)
            self.assertEqual(p5["rollback"]["digest"], self.d2)
            self.assertTrue(tracker.assert_transition("rollback_staged", "rollback_boot", "rollback_boot"))

            report = tracker.generate_report()
            self.assertEqual(report["overall_status"], "PASS")
            self.assertEqual(len(report["phases"]), 5)
            self.assertEqual(len(report["diagnostics"]), 0)

            # Check files were written
            self.assertTrue((work_dir / "lifecycle-summary.json").exists())
            self.assertTrue((work_dir / "lifecycle-report.md").exists())

    def test_failure_diagnostics_on_desktop_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)
            tracker = LifecycleTracker(work_dir)

            s1 = json.dumps({
                "apiVersion": "org.containers.bootc/v1alpha1",
                "status": {
                    "booted": {
                        "image": {"imageDigest": self.d1, "image": {"image": f"{self.img}:testing"}},
                    }
                }
            })
            p1 = tracker.record_phase(
                "upgraded_boot", s1, "FAIL", desktop_ok=False, notes="GDM failed to start"
            )
            self.assertEqual(p1["status"], "FAIL")
            self.assertFalse(p1["desktop_ok"])

            report = tracker.generate_report()
            self.assertEqual(report["overall_status"], "FAIL")
            self.assertGreaterEqual(len(report["diagnostics"]), 1)

            # Verify diagnostic captured the active booted digest
            diag = report["diagnostics"][0]
            self.assertEqual(diag["phase"], "upgraded_boot")
            self.assertEqual(diag["active_booted_digest"], self.d1)
            self.assertIn("Active booted digest", diag["message"])

    def test_failure_diagnostics_on_unexpected_reboot_digest(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)
            tracker = LifecycleTracker(work_dir)

            s1 = json.dumps({
                "apiVersion": "org.containers.bootc/v1alpha1",
                "status": {
                    "booted": {"image": {"imageDigest": self.d1, "image": {"image": self.img}}},
                    "staged": {"image": {"imageDigest": self.d2, "image": {"image": self.img}}},
                }
            })
            tracker.record_phase("upgrade_staged", s1, "PASS")

            # Reboot resulted in the same old d1 instead of d2!
            s2 = json.dumps({
                "apiVersion": "org.containers.bootc/v1alpha1",
                "status": {
                    "booted": {"image": {"imageDigest": self.d1, "image": {"image": self.img}}},
                    "staged": None,
                }
            })
            tracker.record_phase("upgraded_boot", s2, "PASS")

            with self.assertRaises(AssertionError) as ctx:
                tracker.assert_transition("upgrade_staged", "upgraded_boot", "upgraded_boot")
            self.assertIn("Active booted digest", str(ctx.exception))
            self.assertIn("Expected candidate digest", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
