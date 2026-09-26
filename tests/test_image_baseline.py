"""The image-baseline gap: what Bluefin ships that Utah does not."""
import contextlib
import importlib.util
import io
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("image_baseline", ROOT / "scripts/image-baseline.py")
ib = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ib)


class GapTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        for d in ("bluefin", "utah", "dakota"):
            (self.base / d).mkdir()
        self.write("bluefin/rpms.tsv", "gnome-initial-setup\t50.0\ncoreutils\t9.7\ntailscale\t1.9\n")
        self.write("bluefin/surface.tsv", "\n".join([
            "gnome-initial-setup\t/usr/lib/systemd/user/gnome-initial-setup.service",
            "coreutils\t/usr/bin/ls",
            "tailscale\t/usr/bin/tailscale",
            "(unowned)\t/usr/bin/weird",
        ]) + "\n")
        # Utah renames coreutils and moves tailscale, but has both.
        self.write("utah/rpms.tsv", "coreutils-single\t9.7\ntailscale\t1.9\n")
        self.write("utah/surface.tsv", "coreutils-single\t/usr/bin/ls\ntailscale\t/usr/sbin/tailscale\n")
        self.write("dakota/elements.tsv",
                   "# element\tname\tversion\ngnome-build-meta.bst-core-gnome-initial-setup.bst\tgis\t51\n")
        for name, text in (("bluefin/image.txt", "b@sha256:1"), ("utah/image.txt", "u@sha256:2"),
                           ("dakota/source.txt", "run 1")):
            self.write(name, text + "\n")
        self.saved = (ib.BASE, ib.TRIAGE)
        ib.BASE, ib.TRIAGE = self.base, self.base / "triage.toml"
        self.addCleanup(lambda: setattr(ib, "BASE", self.saved[0]) or setattr(ib, "TRIAGE", self.saved[1]))

    def write(self, name, text):
        (self.base / name).write_text(text)

    def check(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            code = ib.check()
        return code, err.getvalue()

    def test_a_package_missing_by_name_and_by_file_is_a_gap(self):
        self.assertEqual(list(ib.gaps()), ["gnome-initial-setup"])

    def test_renamed_or_moved_packages_are_not_gaps(self):
        gaps = ib.gaps()
        self.assertNotIn("coreutils", gaps)   # renamed, file present
        self.assertNotIn("tailscale", gaps)   # same name, file moved
        self.assertNotIn("(unowned)", gaps)

    def test_an_untriaged_gap_fails_the_check(self):
        code, err = self.check()
        self.assertEqual(code, 1)
        self.assertIn("gnome-initial-setup", err)

    def test_a_triaged_gap_passes(self):
        self.write("triage.toml", '[package.gnome-initial-setup]\nstatus = "planned"\nissue = "#261"\n')
        self.assertEqual(self.check()[0], 0)

    def test_an_unknown_status_fails(self):
        self.write("triage.toml", '[package.gnome-initial-setup]\nstatus = "later"\n')
        code, err = self.check()
        self.assertEqual(code, 1)
        self.assertIn("later", err)

    def test_report_names_the_dakota_element(self):
        ib.write_report()
        report = (self.base / "GAP.md").read_text()
        self.assertIn("gnome-build-meta.bst-core-gnome-initial-setup.bst", report)
        self.assertIn("1 user unit", report)


if __name__ == "__main__":
    unittest.main()
