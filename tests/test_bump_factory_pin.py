"""The factory pin bump has to rewrite exactly one line, or not at all.

`ARG PACKAGE_IMAGE_SHA` is the digest of ghcr.io/projectbluefin/utah-packages.
The pin is deliberate -- an image build must be reviewable against the exact
package set it consumed -- but nothing revved it, so the factory published
GNOME 51 finals and this repository kept building against the old digest until
someone noticed by hand (#336). `scripts/bump-factory-pin.py` makes the rev a
routine pull request; these tests pin the behaviour that makes such a script
safe to run unattended on a schedule.

The dangerous failure is not a wrong digest, it is a rewrite that is silently
wrong in some other way: a line that does not compose, a pin replaced twice, a
malformed digest written into the one line every image build reads. The
resolution path is exercised against a stub resolver so the suite needs no
network and no registry credentials.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "bump-factory-pin.py"
CONTAINERFILE = ROOT / "Containerfile"

spec = importlib.util.spec_from_file_location("bump_factory_pin", SCRIPT)
bump = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bump)

OLD = "sha256:" + "1" * 64
NEW = "sha256:" + "2" * 64

CONTAINERFILE_HEADER = f"""\
ARG BASE_IMAGE=quay.io/hummingbird-community/bootc-os:latest@sha256:{'3' * 64}
ARG PACKAGE_IMAGE=ghcr.io/projectbluefin/utah-packages
ARG PACKAGE_IMAGE_SHA={OLD}
ARG PACKAGE_IMAGE_REF=${{PACKAGE_IMAGE}}@${{PACKAGE_IMAGE_SHA}}
ARG COMMON_IMAGE=ghcr.io/projectbluefin/common
"""

REPO_FILE_TEXT = f"""\
# Digest-pinned OCI repository copied from projectbluefin/utah-packages.
# factory-pin: {OLD}
[utah-packages]
# utah-install: true
name=Utah package factory
baseurl=file:///etc/utah-packages
enabled=1
gpgcheck=0
priority=1
"""


class RewriteTests(unittest.TestCase):
    def test_rewrite_moves_only_the_pin_line(self):
        updated = bump.rewrite(CONTAINERFILE_HEADER, NEW)
        self.assertIn(f"ARG PACKAGE_IMAGE_SHA={NEW}", updated)
        self.assertNotIn(OLD, updated)
        # Everything else is byte-identical, so the PR diff is one line.
        before = CONTAINERFILE_HEADER.splitlines()
        after = updated.splitlines()
        self.assertEqual(len(before), len(after))
        differing = [i for i, (a, b) in enumerate(zip(before, after)) if a != b]
        self.assertEqual(differing, [before.index(f"ARG PACKAGE_IMAGE_SHA={OLD}")])

    def test_rewrite_keeps_the_pin_line_parseable_afterwards(self):
        updated = bump.rewrite(CONTAINERFILE_HEADER, NEW)
        self.assertEqual(bump.current_pin(updated), NEW)
        bump.verify_shape(updated)

    def test_rewrite_refuses_a_malformed_digest(self):
        with self.assertRaises(bump.PinError):
            bump.rewrite(CONTAINERFILE_HEADER, "sha256:nothex")

    def test_rewrite_refuses_to_touch_an_unparseable_pin(self):
        broken = CONTAINERFILE_HEADER.replace(OLD, "latest")
        with self.assertRaises(bump.PinError):
            bump.rewrite(broken, NEW)

    def test_rewrite_replaces_only_the_first_occurrence(self):
        doubled = CONTAINERFILE_HEADER + f"ARG OTHER_PIN={OLD}\n"
        updated = bump.rewrite(doubled, NEW)
        self.assertEqual(updated.count(NEW), 1)
        self.assertIn(f"ARG OTHER_PIN={OLD}", updated)

    def test_rewrite_stamp_moves_only_the_stamp_line(self):
        updated = bump.rewrite_stamp(REPO_FILE_TEXT, NEW)
        self.assertIn(f"# factory-pin: {NEW}", updated)
        self.assertNotIn(OLD, updated)
        before = REPO_FILE_TEXT.splitlines()
        after = updated.splitlines()
        self.assertEqual(len(before), len(after))
        differing = [i for i, (a, b) in enumerate(zip(before, after)) if a != b]
        self.assertEqual(differing, [before.index(f"# factory-pin: {OLD}")])

    def test_rewrite_stamp_refuses_a_malformed_digest(self):
        with self.assertRaises(bump.PinError):
            bump.rewrite_stamp(REPO_FILE_TEXT, "sha256:nothex")

    def test_current_stamp_rejects_a_missing_stamp(self):
        with self.assertRaises(bump.PinError):
            bump.current_stamp("[utah-packages]\nenabled=1\n")


class ShapeTests(unittest.TestCase):
    def test_verify_shape_accepts_the_committed_containerfile(self):
        bump.verify_shape(CONTAINERFILE.read_text())

    def test_verify_shape_rejects_a_ref_that_would_not_see_the_bump(self):
        detached = CONTAINERFILE_HEADER.replace(
            "ARG PACKAGE_IMAGE_REF=${PACKAGE_IMAGE}@${PACKAGE_IMAGE_SHA}",
            "ARG PACKAGE_IMAGE_REF=${PACKAGE_IMAGE}:latest")
        with self.assertRaises(bump.PinError):
            bump.verify_shape(detached)

    def test_verify_shape_rejects_a_missing_package_image_arg(self):
        with self.assertRaises(bump.PinError):
            bump.verify_shape("ARG PACKAGE_IMAGE_SHA=" + OLD + "\n")

    def test_current_pin_reports_the_pinned_digest(self):
        self.assertEqual(bump.current_pin(CONTAINERFILE_HEADER), OLD)

    def test_committed_pin_is_a_digest(self):
        self.assertTrue(
            bump.DIGEST_RE.match(bump.current_pin(CONTAINERFILE.read_text())))


class ImageReferenceTests(unittest.TestCase):
    def test_split_image_drops_the_registry_host(self):
        self.assertEqual(
            bump.split_image("ghcr.io/projectbluefin/utah-packages"),
            ("ghcr.io", "projectbluefin/utah-packages"))

    def test_split_image_rejects_a_bare_repository_name(self):
        with self.assertRaises(bump.PinError):
            bump.split_image("utah-packages")


class MainTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "Containerfile"
        self.path.write_text(CONTAINERFILE_HEADER)
        self.repo = Path(self.dir.name) / "packages" / "utah-packages.repo"
        self.repo.parent.mkdir()
        self.repo.write_text(REPO_FILE_TEXT)
        self.addCleanup(self.dir.cleanup)

    def run_main(self, *argv):
        # The script narrates every outcome on stderr so `--print` owns stdout
        # alone; a 400-test suite should not print the script's commentary.
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            return bump.main(["--containerfile", str(self.path), *argv])

    def test_writes_the_resolved_digest(self):
        with mock.patch.object(bump, "resolve_digest", return_value=NEW):
            self.assertEqual(self.run_main(), 0)
        self.assertIn(f"ARG PACKAGE_IMAGE_SHA={NEW}", self.path.read_text())
        self.assertIn(f"# factory-pin: {NEW}", self.repo.read_text())

    def test_a_stamp_that_disagrees_with_the_pin_stops_the_bump(self):
        self.repo.write_text(REPO_FILE_TEXT.replace(OLD, NEW))
        with mock.patch.object(bump, "resolve_digest", return_value="sha256:" + "4" * 64):
            self.assertEqual(self.run_main(), 1)
        self.assertIn(f"ARG PACKAGE_IMAGE_SHA={OLD}", self.path.read_text())
        self.assertIn(f"# factory-pin: {NEW}", self.repo.read_text())

    def test_a_missing_stamp_stops_the_bump(self):
        self.repo.write_text("[utah-packages]\nenabled=1\n")
        with mock.patch.object(bump, "resolve_digest", return_value=NEW):
            self.assertEqual(self.run_main(), 1)
        self.assertIn(f"ARG PACKAGE_IMAGE_SHA={OLD}", self.path.read_text())

    def test_explicit_digest_wins_over_resolution(self):
        def explode(*_args, **_kwargs):
            raise AssertionError("resolution must not run when --digest is given")

        with mock.patch.object(bump, "resolve_digest", explode):
            self.assertEqual(self.run_main("--digest", NEW), 0)
        self.assertIn(f"ARG PACKAGE_IMAGE_SHA={NEW}", self.path.read_text())

    def test_explicit_digest_still_has_to_be_a_digest(self):
        self.assertEqual(self.run_main("--digest", "latest"), 1)
        self.assertIn(OLD, self.path.read_text())

    def test_check_reports_staleness_and_writes_nothing(self):
        with mock.patch.object(bump, "resolve_digest", return_value=NEW):
            self.assertEqual(self.run_main("--check"), 1)
        self.assertIn(OLD, self.path.read_text())

    def test_already_current_pin_is_a_no_op(self):
        with mock.patch.object(bump, "resolve_digest", return_value=OLD):
            self.assertEqual(self.run_main(), 0)
        self.assertIn(f"ARG PACKAGE_IMAGE_SHA={OLD}", self.path.read_text())

    def test_print_reports_the_digest_without_touching_the_file(self):
        with mock.patch.object(bump, "resolve_digest", return_value=NEW):
            self.assertEqual(self.run_main("--print"), 0)
        self.assertIn(OLD, self.path.read_text())

    def test_resolution_failure_is_reported_not_raised(self):
        with mock.patch.object(bump, "resolve_digest",
                               side_effect=bump.PinError("no digest header")):
            self.assertEqual(self.run_main(), 1)
        self.assertIn(OLD, self.path.read_text())

    def test_a_ref_that_ignores_the_pin_stops_the_bump(self):
        self.path.write_text(CONTAINERFILE_HEADER.replace(
            "ARG PACKAGE_IMAGE_REF=${PACKAGE_IMAGE}@${PACKAGE_IMAGE_SHA}",
            "ARG PACKAGE_IMAGE_REF=${PACKAGE_IMAGE}:latest"))
        with mock.patch.object(bump, "resolve_digest", return_value=NEW):
            self.assertEqual(self.run_main(), 1)
        self.assertIn(OLD, self.path.read_text())


class CommittedStateTests(unittest.TestCase):
    """The committed pin is behind the registry today (#336), and that is the
    point: the fix is a weekly pull request, not a hand edit, so the test that
    guards the mechanism is that the script runs against the real file. It is
    the only test here that talks to a registry, so it is opt-in: set
    UTAH_NETWORK_TESTS=1 to keep `just check` offline and fast by default."""

    @unittest.skipUnless(os.environ.get("UTAH_NETWORK_TESTS") == "1",
                         "set UTAH_NETWORK_TESTS=1 to reach the registry")
    def test_script_parses_and_reports_the_committed_pin(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--print"],
            capture_output=True, text=True)
        if result.returncode != 0:
            self.skipTest(f"registry unreachable: {result.stderr.strip()}")
        self.assertTrue(bump.DIGEST_RE.match(result.stdout.strip()))

if __name__ == "__main__":
    unittest.main()
