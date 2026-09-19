"""Behavioral coverage for scripts/verify-multimedia.py's VA-API probe.

The probe runs inside the image build, chained with && in the Containerfile, so
getting its fatal/non-fatal split wrong either breaks every build or silently
stops asserting the codec path. Both directions have bitten this script:
first it failed the build on a headless vainfo, then it was made unconditionally
non-fatal, which threw the assertion away. These pin the intended split.
"""
import importlib.util
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "verify-multimedia.py"

_spec = importlib.util.spec_from_file_location("verify_multimedia", SCRIPT)
verify_multimedia = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(verify_multimedia)

LIBVA = ["/usr/lib64/libva.so.2"]


def globber_for(*, libva=True, render_node=True):
    def globber(pattern):
        if "libva.so" in pattern:
            return LIBVA if libva else []
        if "renderD" in pattern:
            return ["/dev/dri/renderD128"] if render_node else []
        return []
    return globber


def completed(returncode=0, stdout="", stderr=""):
    def runner(*_args, **_kwargs):
        return subprocess.CompletedProcess([], returncode, stdout, stderr)
    return runner


def probe(**kw):
    """Run vaapi_probe with injected environment; returns (failures, notes)."""
    notes = []
    kw.setdefault("which", lambda _: "/usr/bin/vainfo")
    kw.setdefault("globber", globber_for())
    kw.setdefault("runner", completed(0, "VAProfileH264Main"))
    failures = verify_multimedia.vaapi_probe(out=notes.append, **kw)
    return failures, notes


class LibvaTests(unittest.TestCase):
    def test_missing_libva_always_fails(self):
        failures, _ = probe(globber=globber_for(libva=False))
        self.assertIn("libva shared library not present", failures)

    def test_missing_libva_fails_even_without_a_render_node(self):
        """The hardware-independent half must not be skipped during a build."""
        failures, _ = probe(globber=globber_for(libva=False, render_node=False))
        self.assertIn("libva shared library not present", failures)


class ProbeGatingTests(unittest.TestCase):
    def test_no_render_node_skips_without_running_vainfo(self):
        """The image-build case: no /dev/dri, so the probe must not run or fail."""
        ran = []

        def runner(*a, **k):
            ran.append(a)
            return subprocess.CompletedProcess([], 1, "", "failed to initialize display")

        failures, notes = probe(globber=globber_for(render_node=False), runner=runner)
        self.assertEqual(failures, [])
        self.assertEqual(ran, [], "vainfo was executed despite no render node")
        self.assertTrue(any("render node" in n for n in notes), notes)

    def test_absent_vainfo_skips_without_failing(self):
        failures, notes = probe(which=lambda _: None)
        self.assertEqual(failures, [])
        self.assertTrue(any("vainfo not installed" in n for n in notes), notes)

    def test_render_node_present_and_vainfo_failing_is_fatal(self):
        """On real hardware a driver regression must still fail the check."""
        failures, _ = probe(runner=completed(1, "", "failed to initialize display"))
        self.assertIn("vainfo could not initialise a VA-API driver", failures)

    def test_render_node_present_and_vainfo_reporting_no_driver_is_fatal(self):
        failures, _ = probe(runner=completed(0, "vainfo: no driver available", ""))
        self.assertIn("vainfo could not initialise a VA-API driver", failures)

    def test_working_driver_passes_and_says_so(self):
        failures, notes = probe(runner=completed(0, "VAProfileH264Main : VAEntrypointVLD"))
        self.assertEqual(failures, [])
        self.assertTrue(any("initialised a VA-API driver" in n for n in notes), notes)

    def test_benign_error_lines_with_zero_exit_do_not_fail(self):
        """libva logs non-fatal `error:` lines before a fallback driver
        initialises with exit 0; that must not be read as a probe failure."""
        failures, _ = probe(
            runner=completed(0, "VAProfileH264Main", "error: can't connect to X server!")
        )
        self.assertEqual(failures, [])


class FactoryReleaseTests(unittest.TestCase):
    """The release marker must assert a factory (hum<N>.bfin) build, not just
    any Hummingbird-disttag build -- the base/public-hummingbird repo ships the
    same names with a `hum` disttag but without the `.bfin` factory marker."""

    def test_factory_release_matches(self):
        self.assertTrue(verify_multimedia.FACTORY_RELEASE_RE.search("2.24.1-1.hum1.bfin"))

    def test_non_factory_release_rejected(self):
        self.assertFalse(verify_multimedia.FACTORY_RELEASE_RE.search("2.24.0-2.fc39"))

    def test_base_hummingbird_disttag_without_bfin_rejected(self):
        self.assertFalse(verify_multimedia.FACTORY_RELEASE_RE.search("2.24.1-1.hum1.fc39"))


if __name__ == "__main__":
    unittest.main()
