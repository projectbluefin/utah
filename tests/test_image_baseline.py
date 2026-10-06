"""The image-baseline gap: what Bluefin ships that Utah does not.

`gaps()`, `check()` and the report body were already pinned here. The three
functions that talk to the outside world -- `extract()`, `dakota()` and the
`main()` subcommand dispatch -- were not executed by any suite, so the parts
of this script that decide what a baseline *contains* were unverified while
the parts that compare baselines were fully covered.

That is the wrong way round for a snapshot tool. `just baselines` regenerates
the files under baselines/ and `just check-parity` fails the build on a gap
that is not in triage.toml; if `extract()` pins the wrong digest, or
`dakota()` lets the SBOM's source entries through as elements, the comparison
is exact and the inputs are wrong, which reads as a parity change rather than
a tooling bug. The suites below drive both through an injected runner, so the
container and the artifact download are not needed to prove their contracts.
"""
import contextlib
import importlib.util
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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

    def test_report_classifies_missing_firefox_defaults_and_ignores_shipped_assets(self):
        path = "/usr/share/ublue-os/firefox-config/01-bluefin-global.js"
        self.write("bluefin/surface.tsv", f"firefox-defaults\t{path}\n")
        ib.write_report()
        self.assertEqual(ib.gaps(), {"firefox-defaults": [path]})
        self.assertIn("| firefox-defaults | **new** | 1 ublue asset |",
                      (self.base / "GAP.md").read_text())

        # Common's overlay can ship the same asset without owning an RPM.
        self.write("utah/surface.tsv", f"(unowned)\t{path}\n")
        ib.write_report()
        self.assertEqual(ib.gaps(), {})
        self.assertNotIn("| firefox-defaults |", (self.base / "GAP.md").read_text())

    def test_report_renders_the_triage_reason_and_its_issue(self):
        self.write("triage.toml",
                   '[package.gnome-initial-setup]\n'
                   'status = "planned"\nreason = "waiting on the installer"\n'
                   'issue = "#261"\n')
        ib.write_report()
        report = (self.base / "GAP.md").read_text()
        self.assertIn("waiting on the installer (#261)", report)
        self.assertIn("| planned |", report)

    def test_a_reasonless_entry_still_renders_its_issue(self):
        self.write("triage.toml",
                   '[package.gnome-initial-setup]\nstatus = "waived"\nissue = "#9"\n')
        ib.write_report()
        # No reason, so the cell is the bare issue rather than " (#9)".
        self.assertIn("| (#9) |", (self.base / "GAP.md").read_text())

    def test_no_dakota_baseline_leaves_the_column_empty(self):
        (self.base / "dakota/elements.tsv").unlink()
        self.assertEqual(ib.dakota_has("gnome-initial-setup"), "")

    def test_a_package_dakota_does_not_ship_has_no_element(self):
        self.assertEqual(ib.dakota_has("plymouth"), "")

    def test_an_unjunctioned_element_matches_by_exact_name(self):
        self.write("dakota/elements.tsv",
                   "# element\tname\tversion\nplymouth.bst\tplymouth\t24\n")
        self.assertEqual(ib.dakota_has("plymouth"), "plymouth.bst")

    def test_a_closed_gap_is_reported_as_droppable_from_triage(self):
        self.write("triage.toml",
                   '[package.gnome-initial-setup]\nstatus = "planned"\n'
                   '[package.plymouth]\nstatus = "waived"\n')
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = ib.check()
        self.assertEqual(code, 0)
        self.assertIn("drop from triage.toml: plymouth", out.getvalue())


class ExtractTests(unittest.TestCase):
    """`extract` pins the digest and lifts the two TSVs out of the container.

    baselines/*/image.txt is the provenance of every gap the report claims.
    `podman image inspect` returns the digest of the image that was pulled, and
    a tag moves; recording `image@digest` is what makes a refreshed baseline
    attributable to a specific build rather than to whatever :latest meant that
    day.
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.out = Path(tmp.name) / "utah"
        self.calls: list[list[str]] = []
        self.script_body = None

    def runner(self, digest: str):
        def fake_run(argv, *args, **kwargs):
            self.calls.append(argv)
            if argv[:3] == ["podman", "image", "inspect"]:
                return subprocess.CompletedProcess(argv, 0, digest, "")
            mount = next(a for a in argv if a.endswith(":/out:z"))
            shared = Path(mount[: -len(":/out:z")])
            self.script_body = (shared / "x.sh").read_text()
            # Stand in for what EXTRACT writes from inside the image.
            (shared / "rpms.tsv").write_text("bash\t5.3-1\n")
            (shared / "surface.tsv").write_text("bash\t/usr/bin/bash\n")
            return subprocess.CompletedProcess(argv, 0, "", "")
        return fake_run

    def extract(self, image, digest="sha256:abc\n"):
        with mock.patch.object(ib.subprocess, "run", self.runner(digest)):
            ib.extract(image, self.out)

    def test_the_baselines_are_copied_out_of_the_container(self):
        self.extract("ghcr.io/projectbluefin/utah:latest")
        self.assertEqual((self.out / "rpms.tsv").read_text(), "bash\t5.3-1\n")
        self.assertEqual((self.out / "surface.tsv").read_text(), "bash\t/usr/bin/bash\n")

    def test_the_recorded_image_is_pinned_to_the_inspected_digest(self):
        self.extract("ghcr.io/projectbluefin/utah:latest")
        self.assertEqual((self.out / "image.txt").read_text(),
                         "ghcr.io/projectbluefin/utah:latest@sha256:abc\n")

    def test_an_already_pinned_reference_is_not_pinned_twice(self):
        self.extract("ghcr.io/projectbluefin/utah@sha256:old", digest="sha256:new")
        self.assertEqual((self.out / "image.txt").read_text(),
                         "ghcr.io/projectbluefin/utah@sha256:new\n")

    def test_an_unavailable_digest_records_the_reference_unpinned(self):
        # podman prints nothing when it cannot inspect; the baseline must still
        # say which reference it came from rather than claim a bare "@".
        self.extract("ghcr.io/projectbluefin/utah:latest", digest="  \n")
        self.assertEqual((self.out / "image.txt").read_text(),
                         "ghcr.io/projectbluefin/utah:latest\n")

    def test_the_container_runs_the_committed_extract_script(self):
        self.extract("ghcr.io/projectbluefin/utah:latest")
        self.assertEqual(self.script_body, ib.EXTRACT)

    def test_the_script_is_mounted_read_only(self):
        self.extract("ghcr.io/projectbluefin/utah:latest")
        run = self.calls[-1]
        self.assertIn("--rm", run)
        self.assertTrue(any(a.endswith("/x.sh:ro,z") for a in run),
                        f"x.sh must be mounted ro: {run}")

    def test_the_output_directory_is_created(self):
        self.assertFalse(self.out.exists())
        self.extract("ghcr.io/projectbluefin/utah:latest")
        self.assertTrue(self.out.is_dir())


class DakotaSbomTests(unittest.TestCase):
    """`dakota` turns Dakota's SPDX SBOM into an element list.

    Dakota is built with BuildStream, so there is no RPM database to read and
    the SBOM is the only inventory. Its package list mixes BuildStream elements
    with the *sources* of each element, which SPDX emits as "<element>-N". Only
    the ".bst" entries are elements; letting the sources through would inflate
    the Dakota column in GAP.md with names that are not shippable units.
    """

    SBOM = {"packages": [
        {"SPDXID": "SPDXRef-gnome-build-meta.bst-core-gnome-initial-setup.bst",
         "name": "gnome-initial-setup", "versionInfo": "51.0"},
        {"SPDXID": "SPDXRef-gnome-build-meta.bst-core-gnome-initial-setup.bst-0",
         "name": "gnome-initial-setup-tarball", "versionInfo": "51.0"},
        {"SPDXID": "SPDXRef-plymouth.bst", "name": "plymouth", "versionInfo": "24"},
        {"SPDXID": "SPDXRef-base.bst"},
    ]}

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.out = Path(tmp.name) / "dakota"
        self.calls: list[list[str]] = []

    def fake_run(self, argv, *args, **kwargs):
        self.calls.append(argv)
        Path(argv[-1], "dakota.sbom.json").write_text(json.dumps(self.SBOM))
        return subprocess.CompletedProcess(argv, 0, "", "")

    def dakota(self, run="12345"):
        with mock.patch.object(ib.subprocess, "run", self.fake_run):
            ib.dakota(run, self.out)
        return (self.out / "elements.tsv").read_text()

    def test_the_sbom_is_downloaded_from_dakota_s_publish_run(self):
        self.dakota("12345")
        argv = self.calls[0]
        self.assertEqual(argv[:3], ["gh", "run", "download"])
        self.assertIn("12345", argv)
        self.assertEqual(argv[argv.index("-R") + 1], "projectbluefin/dakota")
        self.assertEqual(argv[argv.index("-n") + 1], "sbom-dakota")

    def test_source_entries_are_not_elements(self):
        rows = self.dakota().splitlines()
        self.assertTrue(all(r.split("\t")[0].endswith(".bst")
                            for r in rows if not r.startswith("#")), rows)
        self.assertNotIn("gnome-initial-setup-tarball", self.dakota())

    def test_every_element_keeps_its_name_and_version(self):
        self.assertIn(
            "gnome-build-meta.bst-core-gnome-initial-setup.bst\t"
            "gnome-initial-setup\t51.0", self.dakota())

    def test_an_element_without_name_or_version_still_lands(self):
        # Trailing empties are stripped so the file carries no trailing tabs
        # (the trailing-whitespace pre-commit hook otherwise flips it on every
        # regeneration).
        self.assertIn("base.bst\n", self.dakota())

    def test_the_file_is_sorted_and_carries_the_column_header(self):
        rows = self.dakota().splitlines()
        self.assertEqual(rows[0], "# element\tname\tversion")
        self.assertEqual(rows[1:], sorted(rows[1:]))

    def test_the_provenance_names_the_run_and_the_artifact(self):
        self.dakota("12345")
        source = (self.out / "source.txt").read_text()
        self.assertIn("12345", source)
        self.assertIn("sbom-dakota", source)

    def test_the_element_list_feeds_dakota_has(self):
        # The point of the file: write_report() reads it back by package name.
        self.dakota()
        saved = ib.BASE
        ib.BASE = self.out.parent
        self.addCleanup(lambda: setattr(ib, "BASE", saved))
        self.assertEqual(
            ib.dakota_has("gnome-initial-setup"),
            "gnome-build-meta.bst-core-gnome-initial-setup.bst")


class MainDispatchTests(unittest.TestCase):
    """The subcommands `just baselines` and `just check-parity` call.

    Justfile runs `image-baseline.py check` as a gate and `extract`/`dakota`/
    `gap` to refresh the snapshots. The dispatch had no coverage, so a renamed
    subcommand or a swallowed exit code would only surface as a build that
    stopped failing on untriaged gaps.
    """

    def main(self, *argv):
        with mock.patch.object(ib.sys, "argv", ["image-baseline.py", *argv]):
            return ib.main()

    def test_check_propagates_its_exit_code(self):
        for code in (0, 1):
            with self.subTest(code=code), mock.patch.object(ib, "check", return_value=code):
                self.assertEqual(self.main("check"), code)

    def test_gap_writes_the_report_and_succeeds(self):
        with mock.patch.object(ib, "write_report") as report:
            self.assertEqual(self.main("gap"), 0)
        report.assert_called_once_with()

    def test_extract_forwards_the_image_and_a_path(self):
        with mock.patch.object(ib, "extract") as extract:
            self.assertEqual(self.main("extract", "img:tag", "baselines/utah"), 0)
        image, out = extract.call_args.args
        self.assertEqual(image, "img:tag")
        self.assertEqual(out, Path("baselines/utah"))

    def test_dakota_forwards_the_run_id_and_a_path(self):
        with mock.patch.object(ib, "dakota") as dakota:
            self.assertEqual(self.main("dakota", "12345", "baselines/dakota"), 0)
        run, out = dakota.call_args.args
        self.assertEqual(run, "12345")
        self.assertEqual(out, Path("baselines/dakota"))

    def test_a_subcommand_is_required(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            self.main()
        self.assertNotEqual(raised.exception.code, 0)

    def test_the_justfile_still_calls_these_subcommands(self):
        justfile = (ROOT / "Justfile").read_text()
        for cmd in ("extract", "dakota", "gap", "check"):
            with self.subTest(cmd=cmd):
                self.assertIn(f"image-baseline.py {cmd}", justfile)


if __name__ == "__main__":
    unittest.main()
