"""Unit tests for bootc upgrade and rollback lifecycle validation."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import bootc_lifecycle  # noqa: E402


class TestBootcStatusParsing(unittest.TestCase):
    def test_parse_standard_status(self):
        raw = {
            "status": {
                "booted": {
                    "image": {
                        "image": {"image": "ghcr.io/projectbluefin/utah", "transport": "registry"},
                        "imageDigest": "sha256:1111111111111111111111111111111111111111111111111111111111111111",
                        "version": "41.20260901.0",
                    }
                },
                "staged": {
                    "image": {
                        "image": {"image": "ghcr.io/projectbluefin/utah", "transport": "registry"},
                        "imageDigest": "sha256:2222222222222222222222222222222222222222222222222222222222222222",
                        "version": "41.20260915.0",
                    }
                },
                "rollback": {
                    "image": {
                        "image": {"image": "ghcr.io/projectbluefin/utah", "transport": "registry"},
                        "imageDigest": "sha256:0000000000000000000000000000000000000000000000000000000000000000",
                        "version": "41.20260820.0",
                    }
                },
            }
        }
        deployments = bootc_lifecycle.parse_bootc_status(raw)
        self.assertIn("booted", deployments)
        self.assertIn("staged", deployments)
        self.assertIn("rollback", deployments)

        self.assertEqual(deployments["booted"].digest, "sha256:" + "1" * 64)
        self.assertEqual(deployments["booted"].image, "ghcr.io/projectbluefin/utah")
        self.assertEqual(deployments["staged"].digest, "sha256:" + "2" * 64)
        self.assertEqual(deployments["rollback"].digest, "sha256:" + "0" * 64)

    def test_parse_snake_case_and_flat_image_ref(self):
        raw = {
            "booted": {
                "image": {
                    "image": "localhost/utah:testing",
                    "transport": "containers-storage",
                    "image_digest": "sha256:3333333333333333333333333333333333333333333333333333333333333333",
                }
            }
        }
        deployments = bootc_lifecycle.parse_bootc_status(raw)
        self.assertEqual(deployments["booted"].digest, "sha256:" + "3" * 64)
        self.assertEqual(deployments["booted"].image, "localhost/utah:testing")
        self.assertEqual(deployments["booted"].transport, "containers-storage")

    def test_invalid_json_raises(self):
        with self.assertRaises(ValueError):
            bootc_lifecycle.parse_bootc_status("not-json{")
        with self.assertRaises(ValueError):
            bootc_lifecycle.parse_bootc_status(123)  # type: ignore


class TestLifecyclePhaseTransitions(unittest.TestCase):
    def setUp(self):
        self.d_base = "sha256:" + "a" * 64
        self.d_cand = "sha256:" + "b" * 64

    def test_validate_baseline_success(self):
        raw = {
            "status": {
                "booted": {
                    "image": {
                        "image": "ghcr.io/projectbluefin/utah",
                        "imageDigest": self.d_base,
                    }
                }
            }
        }
        ok, msg, diag = bootc_lifecycle.validate_phase_transition("baseline", raw)
        self.assertTrue(ok)
        self.assertIn("Baseline deployment active", msg)
        self.assertEqual(diag["booted"]["digest"], self.d_base)

    def test_validate_baseline_missing_booted(self):
        ok, msg, _ = bootc_lifecycle.validate_phase_transition("baseline", {"status": {}})
        self.assertFalse(ok)
        self.assertIn("No booted deployment found", msg)

    def test_validate_staged_success(self):
        raw = {
            "status": {
                "booted": {
                    "image": {"image": "ghcr.io/projectbluefin/utah", "imageDigest": self.d_base}
                },
                "staged": {
                    "image": {"image": "ghcr.io/projectbluefin/utah", "imageDigest": self.d_cand}
                },
            }
        }
        ok, msg, _ = bootc_lifecycle.validate_phase_transition(
            "staged", raw, baseline_digest=self.d_base, candidate_digest=self.d_cand
        )
        self.assertTrue(ok)
        self.assertIn("Upgrade staged successfully", msg)

    def test_validate_staged_requires_candidate_digest(self):
        raw = {
            "status": {
                "booted": {
                    "image": {"image": "ghcr.io/projectbluefin/utah", "imageDigest": self.d_base}
                },
                "staged": {
                    "image": {"image": "ghcr.io/projectbluefin/utah", "imageDigest": self.d_cand}
                },
            }
        }
        ok, msg, _ = bootc_lifecycle.validate_phase_transition(
            "staged", raw, baseline_digest=self.d_base, candidate_digest=None
        )
        self.assertFalse(ok)
        self.assertIn("Candidate digest is required", msg)

    def test_validate_upgraded_requires_candidate_digest(self):
        raw = {
            "status": {
                "booted": {
                    "image": {"image": "ghcr.io/projectbluefin/utah", "imageDigest": self.d_cand}
                },
                "rollback": {
                    "image": {"image": "ghcr.io/projectbluefin/utah", "imageDigest": self.d_base}
                },
            }
        }
        ok, msg, _ = bootc_lifecycle.validate_phase_transition(
            "upgraded", raw, baseline_digest=self.d_base, candidate_digest=None
        )
        self.assertFalse(ok)
        self.assertIn("Candidate digest is required", msg)

    def test_validate_staged_atomic_violation(self):
        # If booted digest changed before reboot, atomic staging guarantee was broken
        raw = {
            "status": {
                "booted": {
                    "image": {"image": "ghcr.io/projectbluefin/utah", "imageDigest": self.d_cand}
                },
                "staged": {
                    "image": {"image": "ghcr.io/projectbluefin/utah", "imageDigest": self.d_cand}
                },
            }
        }
        ok, msg, _ = bootc_lifecycle.validate_phase_transition(
            "staged", raw, baseline_digest=self.d_base, candidate_digest=self.d_cand
        )
        self.assertFalse(ok)
        self.assertIn("Atomic guarantee violated", msg)

    def test_validate_staged_mismatch_candidate(self):
        raw = {
            "status": {
                "booted": {
                    "image": {"image": "ghcr.io/projectbluefin/utah", "imageDigest": self.d_base}
                },
                "staged": {
                    "image": {"image": "ghcr.io/projectbluefin/utah", "imageDigest": "sha256:other"}
                },
            }
        }
        ok, msg, _ = bootc_lifecycle.validate_phase_transition(
            "staged", raw, baseline_digest=self.d_base, candidate_digest=self.d_cand
        )
        self.assertFalse(ok)
        self.assertIn("does not match candidate digest", msg)

    def test_validate_upgraded_success(self):
        raw = {
            "status": {
                "booted": {
                    "image": {"image": "ghcr.io/projectbluefin/utah", "imageDigest": self.d_cand}
                },
                "rollback": {
                    "image": {"image": "ghcr.io/projectbluefin/utah", "imageDigest": self.d_base}
                },
            }
        }
        ok, msg, _ = bootc_lifecycle.validate_phase_transition(
            "upgraded", raw, baseline_digest=self.d_base, candidate_digest=self.d_cand
        )
        self.assertTrue(ok)
        self.assertIn("Upgraded deployment active", msg)

    def test_validate_upgraded_missing_rollback(self):
        raw = {
            "status": {
                "booted": {
                    "image": {"image": "ghcr.io/projectbluefin/utah", "imageDigest": self.d_cand}
                }
            }
        }
        ok, msg, _ = bootc_lifecycle.validate_phase_transition(
            "upgraded", raw, baseline_digest=self.d_base, candidate_digest=self.d_cand
        )
        self.assertFalse(ok)
        self.assertIn("Rollback deployment missing", msg)

    def test_validate_rollback_success(self):
        raw = {
            "status": {
                "booted": {
                    "image": {"image": "ghcr.io/projectbluefin/utah", "imageDigest": self.d_base}
                },
                "rollback": {
                    "image": {"image": "ghcr.io/projectbluefin/utah", "imageDigest": self.d_cand}
                },
            }
        }
        ok, msg, _ = bootc_lifecycle.validate_phase_transition(
            "rollback", raw, baseline_digest=self.d_base, candidate_digest=self.d_cand
        )
        self.assertTrue(ok)
        self.assertIn("Rollback successfully restored baseline", msg)

    def test_validate_rollback_failed_restoration(self):
        # Booted is still candidate digest, rollback failed
        raw = {
            "status": {
                "booted": {
                    "image": {"image": "ghcr.io/projectbluefin/utah", "imageDigest": self.d_cand}
                }
            }
        }
        ok, msg, _ = bootc_lifecycle.validate_phase_transition(
            "rollback", raw, baseline_digest=self.d_base
        )
        self.assertFalse(ok)
        self.assertIn("Rollback verification failed", msg)


class TestDiagnosticsAndReporting(unittest.TestCase):
    def test_record_and_summary(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ev_dir = Path(tmpdir)
            d1 = "sha256:" + "1" * 64
            d2 = "sha256:" + "2" * 64

            bootc_lifecycle.record_phase_diagnostics(
                ev_dir, "baseline", "initial-install", d1, "PASS"
            )
            bootc_lifecycle.record_phase_diagnostics(
                ev_dir, "staged", "candidate-staged", d2, "PASS"
            )
            bootc_lifecycle.record_phase_diagnostics(
                ev_dir, "upgraded", "candidate-booted", d2, "PASS"
            )
            bootc_lifecycle.record_phase_diagnostics(
                ev_dir, "rollback", "baseline-restored", d1, "PASS"
            )

            summary = bootc_lifecycle.generate_lifecycle_summary(ev_dir)
            self.assertEqual(summary["status"], "PASS")
            self.assertEqual(len(summary["phases"]), 4)

    def test_failure_summary_formatting(self):
        text = bootc_lifecycle.format_failure_summary(
            phase="upgraded",
            active_deployment="candidate",
            active_digest="sha256:abc",
            expected_digest="sha256:def",
            reason="Booted into panic",
        )
        self.assertIn("BOOTC LIFECYCLE TEST FAILURE", text)
        self.assertIn("Phase:             upgraded", text)
        self.assertIn("Active Deployment: candidate", text)
        self.assertIn("Active Digest:     sha256:abc", text)
        self.assertIn("Expected Digest:   sha256:def", text)
        self.assertIn("Failure Reason:    Booted into panic", text)


class TestImageRepository(unittest.TestCase):
    def test_strips_tag_and_digest(self):
        self.assertEqual(
            bootc_lifecycle.image_repository("ghcr.io/projectbluefin/utah:testing"),
            "ghcr.io/projectbluefin/utah",
        )
        self.assertEqual(
            bootc_lifecycle.image_repository("ghcr.io/projectbluefin/utah@sha256:" + "a" * 64),
            "ghcr.io/projectbluefin/utah",
        )
        self.assertEqual(
            bootc_lifecycle.image_repository("ghcr.io/projectbluefin/utah:testing@sha256:" + "a" * 64),
            "ghcr.io/projectbluefin/utah",
        )

    def test_preserves_registry_port_and_bare_names(self):
        self.assertEqual(
            bootc_lifecycle.image_repository("localhost:5000/utah:testing"),
            "localhost:5000/utah",
        )
        self.assertEqual(bootc_lifecycle.image_repository("utah:testing"), "utah")
        self.assertEqual(bootc_lifecycle.image_repository("  "), "")


class TestCliInterface(unittest.TestCase):
    def test_cli_extract_digest(self):
        data = json.dumps({
            "status": {
                "booted": {
                    "image": {
                        "image": "ghcr.io/projectbluefin/utah",
                        "imageDigest": "sha256:testdigest",
                    }
                }
            }
        })
        proc = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "bootc_lifecycle.py"), "extract-digest", "--status", "-", "--slot", "booted"],
            input=data,
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(proc.stdout.strip(), "sha256:testdigest")

    def test_cli_validate_phase(self):
        data = json.dumps({
            "status": {
                "booted": {
                    "image": {
                        "image": "ghcr.io/projectbluefin/utah",
                        "imageDigest": "sha256:testdigest",
                    }
                }
            }
        })
        proc = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "bootc_lifecycle.py"),
                "validate-phase",
                "baseline",
                "--status",
                "-",
            ],
            input=data,
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertIn("PASS:", proc.stdout)

    def test_cli_extract_image(self):
        data = json.dumps({
            "status": {
                "booted": {
                    "image": {
                        "image": "ghcr.io/projectbluefin/utah:testing",
                        "imageDigest": "sha256:testdigest",
                    }
                }
            }
        })
        script = str(ROOT / "scripts" / "bootc_lifecycle.py")
        proc = subprocess.run(
            [sys.executable, script, "extract-image", "--status", "-", "--slot", "booted"],
            input=data,
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(proc.stdout.strip(), "ghcr.io/projectbluefin/utah:testing")

        proc = subprocess.run(
            [sys.executable, script, "extract-image", "--status", "-", "--slot", "booted", "--repository"],
            input=data,
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(proc.stdout.strip(), "ghcr.io/projectbluefin/utah")

    def test_cli_extract_image_missing_slot_fails(self):
        proc = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "bootc_lifecycle.py"),
                "extract-image",
                "--status",
                "-",
                "--slot",
                "staged",
            ],
            input=json.dumps({"status": {}}),
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 1)

    def test_cli_image_repository(self):
        proc = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "bootc_lifecycle.py"),
                "image-repository",
                "--ref",
                "ghcr.io/projectbluefin/utah@sha256:" + "b" * 64,
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(proc.stdout.strip(), "ghcr.io/projectbluefin/utah")


if __name__ == "__main__":
    unittest.main()
