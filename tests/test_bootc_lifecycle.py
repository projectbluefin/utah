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
        self.i_cand = "ghcr.io/projectbluefin/utah@" + self.d_cand

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
            "staged", raw, baseline_digest=self.d_base, candidate_image=self.i_cand
        )
        self.assertTrue(ok)
        self.assertIn("Upgrade staged successfully", msg)

    def test_validate_staged_requires_candidate_image(self):
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
        self.assertFalse(ok)
        self.assertIn("Candidate image is required", msg)

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
            "staged", raw, baseline_digest=self.d_base, candidate_image=self.i_cand
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
            "staged", raw, baseline_digest=self.d_base, candidate_image=self.i_cand
        )
        self.assertFalse(ok)
        self.assertIn("does not match candidate digest", msg)

    def test_validate_staged_rejects_other_repository(self):
        raw = {
            "status": {
                "booted": {
                    "image": {"image": "ghcr.io/projectbluefin/utah", "imageDigest": self.d_base}
                },
                "staged": {
                    "image": {"image": "ghcr.io/example/other:testing", "imageDigest": self.d_cand}
                },
            }
        }
        ok, msg, _ = bootc_lifecycle.validate_phase_transition(
            "staged", raw, baseline_digest=self.d_base, candidate_image=self.i_cand
        )
        self.assertFalse(ok)
        self.assertIn("is not from candidate repository", msg)

    def test_validate_staged_tag_candidate_matches_repository(self):
        raw = {
            "status": {
                "booted": {
                    "image": {"image": "ghcr.io/projectbluefin/utah:testing", "imageDigest": self.d_base}
                },
                "staged": {
                    "image": {"image": "ghcr.io/projectbluefin/utah:stable", "imageDigest": self.d_cand}
                },
            }
        }
        ok, _, _ = bootc_lifecycle.validate_phase_transition(
            "staged", raw, baseline_digest=self.d_base, candidate_image="ghcr.io/projectbluefin/utah:stable"
        )
        self.assertTrue(ok)

    def test_validate_staged_rejects_baseline_digest(self):
        # A different ref that resolves to the booted image is still staged by
        # bootc switch; staging it must not count as an upgrade.
        raw = {
            "status": {
                "booted": {
                    "image": {"image": "ghcr.io/projectbluefin/utah:testing", "imageDigest": self.d_base}
                },
                "staged": {
                    "image": {"image": "ghcr.io/projectbluefin/utah:stable", "imageDigest": self.d_base}
                },
            }
        }
        ok, msg, _ = bootc_lifecycle.validate_phase_transition(
            "staged", raw, baseline_digest=self.d_base, candidate_image="ghcr.io/projectbluefin/utah:stable"
        )
        self.assertFalse(ok)
        self.assertIn("is the baseline digest", msg)

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


# --- Boot Loader Specification (BLS) entry parsing and validation ---
#
# bootc-managed installations write systemd-boot BLS Type 1 entries under
# /boot/loader/entries on the EFI System Partition. Each entry carries an
# `ostree=/ostree/boot.N/<stateroot>/<bootcsum>/<serial>` karg in the
# `options` line and the kernel path in `linux`. The `<bootcsum>` segment
# is the kernel+initramfs layout hash
# (`ostree_deployment_get_bootcsum` in libostree), NOT the ostree commit
# checksum; bootc's BootEntryOstree JSON does not expose it. The validator
# therefore anchors on the deployment-unique `(stateroot, deploy_serial)`
# pair and counts entries per group rather than matching `<bootcsum>` to
# a commit. The lifecycle suite uses these entries to confirm that
# finalize-staged actually wrote the boot manager side, not just the
# bootc status metadata (the issue the regression previously slipped past).


class TestLoaderEntryParsing(unittest.TestCase):
    def test_parse_simple_entry(self):
        content = (
            "title Utah (41.20260915.0) 41.20260915.0\n"
            "version 0\n"
            "machine-id 0123456789abcdef0123456789abcdef\n"
            "linux /ostree/utah-41.20260915.0abcdef.../vmlinuz-6.10.0-utah\n"
            "initrd /ostree/utah-41.20260915.0abcdef.../initramfs-6.10.0-utah.img\n"
            "options root=UUID=0000 ro ostree=/ostree/boot.0/utah/41.20260915.0abcdef/0\n"
        )
        entry = bootc_lifecycle.parse_loader_entry(
            "ostree-utah-0.conf", content
        )
        self.assertEqual(entry.fields["title"], "Utah (41.20260915.0) 41.20260915.0")
        self.assertEqual(entry.fields["version"], "0")
        self.assertEqual(entry.fields["linux"], "/ostree/utah-41.20260915.0abcdef.../vmlinuz-6.10.0-utah")
        self.assertEqual(entry.fields["initrd"], "/ostree/utah-41.20260915.0abcdef.../initramfs-6.10.0-utah.img")
        self.assertEqual(entry.fields["options"], "root=UUID=0000 ro ostree=/ostree/boot.0/utah/41.20260915.0abcdef/0")

    def test_strips_comments_and_blank_lines(self):
        content = (
            "# Boot Loader Specification Type #1 entry\n"
            "\n"
            "title Utah\n"
            "# inline setting with continuation\n"
            "  trailing-continuation-of-title\n"
            "version 0\n"
            "linux /vmlinuz\n"
        )
        entry = bootc_lifecycle.parse_loader_entry("ostree-utah-0.conf", content)
        # Continuation lines (those starting with whitespace) belong to the
        # prior key per the BLS spec; the parser must not invent a new key
        # for them and must append them to the previous value verbatim.
        self.assertEqual(entry.fields["title"], "Utah trailing-continuation-of-title")
        self.assertNotIn("trailing-continuation-of-title", entry.fields)
        self.assertEqual(entry.fields["version"], "0")
        self.assertEqual(entry.fields["linux"], "/vmlinuz")

    def test_first_value_wins(self):
        # bootc always emits a single value per key, but tolerate a stray
        # duplicate by treating the first as authoritative so a downstream
        # comment-like line cannot shadow the real setting.
        content = (
            "title Should-win\n"
            "title Should-lose\n"
            "linux /vmlinuz-first\n"
            "linux /vmlinuz-second\n"
        )
        entry = bootc_lifecycle.parse_loader_entry("e.conf", content)
        self.assertEqual(entry.fields["title"], "Should-win")
        self.assertEqual(entry.fields["linux"], "/vmlinuz-first")


class TestLoaderListingParsing(unittest.TestCase):
    def test_parses_multiple_entries(self):
        listing = (
            "=== ENTRY /boot/loader/entries/ostree-utah-0.conf ===\n"
            "title Utah baseline\n"
            "version 0\n"
            "linux /vmlinuz-baseline\n"
            "initrd /initramfs-baseline.img\n"
            "options ro ostree=/ostree/boot.0/utah/aaaa/0\n"
            "=== END ===\n"
            "=== ENTRY /boot/loader/entries/ostree-utah-1.conf ===\n"
            "title Utah staged\n"
            "version 1\n"
            "linux /vmlinuz-staged\n"
            "initrd /initramfs-staged.img\n"
            "options ro ostree=/ostree/boot.0/utah/bbbb/0\n"
            "=== END ===\n"
        )
        entries = bootc_lifecycle.parse_loader_listing(listing)
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0].filename, "/boot/loader/entries/ostree-utah-0.conf")
        self.assertEqual(entries[0].fields["linux"], "/vmlinuz-baseline")
        self.assertEqual(entries[1].filename, "/boot/loader/entries/ostree-utah-1.conf")
        self.assertEqual(entries[1].fields["linux"], "/vmlinuz-staged")

    def test_empty_listing(self):
        self.assertEqual(bootc_lifecycle.parse_loader_listing(""), [])
        self.assertEqual(bootc_lifecycle.parse_loader_listing("\n\n"), [])

    def test_recovers_entry_without_trailing_sentinel(self):
        # A truncated SSH pipe is the realistic failure mode; the parser must
        # still surface the partial entry rather than swallow it, otherwise
        # a network blip would silently demote the validation to "nothing to
        # check".
        listing = (
            "=== ENTRY /boot/loader/entries/ostree-utah-2.conf ===\n"
            "title Utah recovered\n"
            "version 0\n"
            "linux /vmlinuz\n"
        )
        entries = bootc_lifecycle.parse_loader_listing(listing)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].filename, "/boot/loader/entries/ostree-utah-2.conf")
        self.assertEqual(entries[0].fields["title"], "Utah recovered")


def _make_status_with_ostree(checksums, *, serial_map=None):
    """Build a bootc status JSON whose deployments carry the given ostree checksums.

    `checksums` maps a slot to its ostree checksum; missing slots are absent
    from the JSON, just as `bootc status` omits absent deployments. The image
    digest is a separate value from the ostree checksum so that the tests
    exercise both layers independently. `serial_map` lets a test give each
    slot a distinct `deploySerial` (the boot-manager match key); defaults to
    0 for every slot, which keeps the legacy single-slot tests working.
    """
    serials = serial_map or {}

    def entry(slot, csum):
        return {
            "image": {
                "image": "ghcr.io/projectbluefin/utah",
                "transport": "registry",
                "imageDigest": f"sha256:img-{slot:0>60}",
            },
            "ostree": {
                "checksum": csum,
                "stateroot": "utah",
                "deploySerial": serials.get(slot, 0),
            },
            "pinned": False,
        }

    return {"status": {slot: entry(slot, csum) for slot, csum in checksums.items()}}


class TestBootmgrValidation(unittest.TestCase):
    def setUp(self):
        self.csum_base = "1.0" + "a" * 62
        self.csum_cand = "1.0" + "b" * 62

    def _entry_content(self, ostree_csum, linux_path="/vmlinuz-utah", initrd_path="/initramfs-utah.img", options_extra="", *, serial=0):
        # ostree writes an `ostree=/ostree/boot.N/<stateroot>/<bootcsum>/<serial>`
        # karg into every BLS entry it produces
        # (src/libostree/ostree-sysroot-deploy.c). The validator anchors on
        # `(stateroot, deploy_serial)`, the deployment-unique pair; the
        # `<bootcsum>` segment in the middle is the kernel+initramfs layout
        # hash (NOT the commit checksum the deployment object exposes as
        # `ostree.checksum`), so the test fixture uses a placeholder there and
        # asserts the validator does not look for the commit in the path.
        # `serial` matches the deployment's `ostree.deploySerial` for the
        # slot the entry is meant to anchor.
        return (
            f"title Utah {ostree_csum[:8]}\n"
            "version 0\n"
            "machine-id 0123456789abcdef0123456789abcdef\n"
            f"linux {linux_path}\n"
            f"initrd {initrd_path}\n"
            f"options root=UUID=0000 ro ostree=/ostree/boot.0/utah/{ostree_csum[:62]}/{serial}{options_extra}\n"
        )

    def _listing(self, entries):
        parts = []
        for filename, content in entries:
            parts.append(f"=== ENTRY {filename} ===\n{content}\n=== END ===\n")
        return "".join(parts)

    def test_baseline_and_staged_each_have_entries(self):
        status = _make_status_with_ostree(
            {"booted": self.csum_base, "staged": self.csum_cand},
            serial_map={"booted": 0, "staged": 1},
        )
        listing = self._listing([
            (
                f"/boot/loader/entries/ostree-utah-{self.csum_base[:8]}.conf",
                self._entry_content(self.csum_base, serial=0),
            ),
            (
                f"/boot/loader/entries/ostree-utah-{self.csum_cand[:8]}.conf",
                self._entry_content(self.csum_cand, linux_path="/vmlinuz-staged", serial=1),
            ),
        ])
        ok, msg, diag = bootc_lifecycle.validate_bootmgr_entries(status, listing)
        self.assertTrue(ok, msg)
        self.assertEqual(diag["matches"]["booted"]["filename"], f"/boot/loader/entries/ostree-utah-{self.csum_base[:8]}.conf")
        self.assertEqual(diag["matches"]["staged"]["filename"], f"/boot/loader/entries/ostree-utah-{self.csum_cand[:8]}.conf")

    def test_missing_entry_for_staged_fails(self):
        # The regression the issue describes: bootc reports a staged
        # deployment, but no BLS entry exists for it, so the next reboot
        # silently boots the old deployment.
        status = _make_status_with_ostree(
            {"booted": self.csum_base, "staged": self.csum_cand},
            serial_map={"booted": 0, "staged": 1},
        )
        listing = self._listing([
            (
                f"/boot/loader/entries/ostree-utah-{self.csum_base[:8]}.conf",
                self._entry_content(self.csum_base, serial=0),
            ),
        ])
        ok, msg, diag = bootc_lifecycle.validate_bootmgr_entries(status, listing)
        self.assertFalse(ok)
        self.assertIn("staged", msg)
        self.assertEqual(diag["missing"][0]["slot"], "staged")

    def test_entry_without_linux_fails(self):
        status = _make_status_with_ostree({"booted": self.csum_base})
        listing = self._listing([
            (
                f"/boot/loader/entries/ostree-utah-{self.csum_base[:8]}.conf",
                f"title Utah {self.csum_base[:8]}\nversion 0\noptions root=UUID=0000 ostree=/ostree/boot.0/utah/{self.csum_base}/0\n",
            ),
        ])
        ok, msg, diag = bootc_lifecycle.validate_bootmgr_entries(status, listing)
        self.assertFalse(ok)
        self.assertIn("linux", msg)
        self.assertEqual(diag["malformed"][0]["slot"], "booted")

    def test_collision_when_two_commits_share_serial(self):
        # ostree allocates `deployserial` per (osname, commit), not per
        # deployment: two distinct commits with no prior deployment at that
        # commit both receive serial 0. A purely-by-serial matcher would
        # then satisfy both deployments with a single BLS entry and
        # silently miss a missing-entry regression. The validator must
        # therefore count entries per (stateroot, deploy_serial) group and
        # fail when the count of entries is below the count of
        # deployments in the group.
        status = _make_status_with_ostree(
            {"booted": self.csum_base, "staged": self.csum_cand},
            # Both commits at serial 0 -- the realistic post-upgrade state.
            serial_map={"booted": 0, "staged": 0},
        )
        # Only one BLS entry exists, but two deployments expect one each.
        listing = self._listing([
            (
                f"/boot/loader/entries/ostree-utah-{self.csum_base[:8]}.conf",
                self._entry_content(self.csum_base, serial=0),
            ),
        ])
        ok, msg, diag = bootc_lifecycle.validate_bootmgr_entries(
            status, listing, expected_slots=("booted", "staged")
        )
        self.assertFalse(ok)
        # Both deployments reported missing -- the diagnostic must name
        # both slots so the failure is debuggable.
        missing_slots = {m["slot"] for m in diag["missing"]}
        self.assertEqual(missing_slots, {"booted", "staged"})
        # And both deployments expect a serial-0 entry that wasn't there.
        for m in diag["missing"]:
            self.assertEqual(m["deploy_serial"], 0)
            self.assertEqual(m["stateroot"], "utah")

    def test_two_commits_at_same_serial_each_have_entry(self):
        # Same setup as the collision test, but both BLS entries exist
        # (one per commit). The validator must accept this -- this is the
        # realistic post-upgrade state, where baseline is rollback and
        # staged becomes booted on next reboot.
        status = _make_status_with_ostree(
            {"booted": self.csum_base, "rollback": self.csum_cand},
            serial_map={"booted": 0, "rollback": 0},
        )
        listing = self._listing([
            (
                f"/boot/loader/entries/ostree-utah-{self.csum_base[:8]}.conf",
                self._entry_content(self.csum_base, serial=0),
            ),
            (
                f"/boot/loader/entries/ostree-utah-{self.csum_cand[:8]}.conf",
                self._entry_content(self.csum_cand, linux_path="/vmlinuz-staged", serial=0),
            ),
        ])
        ok, msg, diag = bootc_lifecycle.validate_bootmgr_entries(
            status, listing, expected_slots=("booted", "rollback")
        )
        self.assertTrue(ok, msg)
        # Each deployment matched a different entry -- the diagnostic
        # filenames must be distinct so a reader can tell which is which.
        booted_match = diag["matches"]["booted"]["filename"]
        rollback_match = diag["matches"]["rollback"]["filename"]
        self.assertNotEqual(booted_match, rollback_match)

    def test_rollback_slot_is_optional(self):
        # After a clean rollback the rollback slot may be empty; the
        # validator must not flag that as a failure because bootc only
        # populates it when the next reboot is queued for it.
        status = _make_status_with_ostree({"booted": self.csum_base})
        listing = self._listing([
            (
                f"/boot/loader/entries/ostree-utah-{self.csum_base[:8]}.conf",
                self._entry_content(self.csum_base),
            ),
        ])
        ok, msg, _ = bootc_lifecycle.validate_bootmgr_entries(status, listing)
        self.assertTrue(ok, msg)

    def test_skipped_slot_via_expected_slots(self):
        # During phase 1 (baseline) only the booted deployment is required,
        # so a harness that already knows staged is unset must not have the
        # validator claim otherwise.
        status = _make_status_with_ostree({"booted": self.csum_base})
        listing = self._listing([
            (
                f"/boot/loader/entries/ostree-utah-{self.csum_base[:8]}.conf",
                self._entry_content(self.csum_base),
            ),
        ])
        ok, msg, _ = bootc_lifecycle.validate_bootmgr_entries(
            status, listing, expected_slots=("booted",)
        )
        self.assertTrue(ok, msg)

    def test_options_ostree_path_is_the_match_key(self):
        # ostree carries the deployment's commit checksum only inside the
        # `options` line's `ostree=/ostree/boot.N/<stateroot>/<bootcsum>/<serial>`
        # path (where `<bootcsum>` is the kernel+initramfs layout hash,
        # NOT the commit). `version` is the integer deployment index and
        # the filename is `ostree-<index>-<stateroot>.conf`; neither
        # carries the commit. A status JSON without an ostree block
        # cannot anchor the match because nothing else is
        # deployment-specific in the entry.
        status = {
            "status": {
                "booted": {
                    "image": {
                        "image": "ghcr.io/projectbluefin/utah",
                        "imageDigest": "sha256:" + "9" * 64,
                    }
                }
            }
        }
        listing = self._listing([
            (
                f"/boot/loader/entries/ostree-utah-{self.csum_base[:8]}.conf",
                self._entry_content(self.csum_base),
            ),
        ])
        ok, msg, _ = bootc_lifecycle.validate_bootmgr_entries(
            status, listing, expected_slots=("booted",)
        )
        self.assertFalse(ok)  # No ostree data to match against

        # When the status JSON does carry the checksum, the entry's
        # options line is the only place to find it back.
        full_status = _make_status_with_ostree({"booted": self.csum_base})
        ok, msg, _ = bootc_lifecycle.validate_bootmgr_entries(
            full_status, listing, expected_slots=("booted",)
        )
        self.assertTrue(ok, msg)

    def test_missing_kernel_on_disk_fails_validation(self) -> None:
        # The harness emits STAT lines that mark kernel/initrd paths as
        # present or missing on disk. A BLS entry that points at a
        # pruned kernel has a syntactically valid `linux` line but a
        # missing file, and the validator must surface that as a
        # malformed entry rather than passing on syntax alone.
        status = _make_status_with_ostree({"booted": self.csum_base})
        filename = f"/boot/loader/entries/ostree-utah-{self.csum_base[:8]}.conf"
        body = self._entry_content(self.csum_base)
        listing = (
            f"=== ENTRY {filename} ===\n{body}\n=== END ===\n"
            f"STAT {filename} /vmlinuz-utah missing\n"
            f"STAT {filename} /initramfs-utah.img present\n"
        )
        ok, msg, diag = bootc_lifecycle.validate_bootmgr_entries(
            status, listing, expected_slots=("booted",)
        )
        self.assertFalse(ok, msg)
        self.assertIn("missing files", msg)
        self.assertIn("/vmlinuz-utah", msg)
        reasons = [r["reason"] for r in diag["malformed"]]
        self.assertIn("missing on disk", reasons)


class TestBootmgrCli(unittest.TestCase):
    def _run_with_files(self, *args, status, listing):
        # The CLI takes two stdin-or-file arguments, so write each to a
        # temporary file and pass those paths. stdin can only be one of
        # them at a time, and conflating the two streams is the easiest way
        # to test the wrong code path.
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as sfp, tempfile.NamedTemporaryFile(
            "w", suffix=".txt", delete=False
        ) as lfp:
            sfp.write(status)
            sfp.flush()
            lfp.write(listing)
            lfp.flush()
            try:
                proc = subprocess.run(
                    [
                        sys.executable,
                        str(ROOT / "scripts" / "bootc_lifecycle.py"),
                        *args,
                        "--status",
                        sfp.name,
                        "--listing",
                        lfp.name,
                    ],
                    capture_output=True,
                    text=True,
                )
            finally:
                Path(sfp.name).unlink(missing_ok=True)
                Path(lfp.name).unlink(missing_ok=True)
        return proc

    def test_cli_validate_bootmgr_pass(self):
        csum = "1.0" + "a" * 62
        status = json.dumps(_make_status_with_ostree({"booted": csum}))
        listing = (
            f"=== ENTRY /boot/loader/entries/ostree-utah-{csum[:8]}.conf ===\n"
            f"title Utah\nversion 0\nlinux /vmlinuz\ninitrd /initrd.img\noptions ro ostree=/ostree/boot.0/utah/{csum}/0\n"
            f"=== END ===\n"
        )
        proc = self._run_with_files(
            "validate-bootmgr",
            "--slots",
            "booted",
            status=status,
            listing=listing,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("PASS:", proc.stdout)

    def test_cli_validate_bootmgr_fail(self):
        csum_base = "1.0" + "a" * 62
        csum_cand = "1.0" + "b" * 62
        status = json.dumps(
            _make_status_with_ostree(
                {"booted": csum_base, "staged": csum_cand},
                serial_map={"booted": 0, "staged": 1},
            )
        )
        listing = (
            f"=== ENTRY /boot/loader/entries/ostree-utah-{csum_base[:8]}.conf ===\n"
            f"title Utah baseline\nversion 0\nlinux /vmlinuz\ninitrd /initrd.img\noptions ro ostree=/ostree/boot.0/utah/{csum_base}/0\n"
            f"=== END ===\n"
        )
        proc = self._run_with_files(
            "validate-bootmgr",
            status=status,
            listing=listing,
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("staged", proc.stderr)

    def test_cli_validate_bootmgr_rejects_double_stdin(self):
        proc = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "bootc_lifecycle.py"),
                "validate-bootmgr",
                "--status",
                "-",
                "--listing",
                "-",
            ],
            input="{}",
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("cannot both read from stdin", proc.stderr)


if __name__ == "__main__":
    unittest.main()
