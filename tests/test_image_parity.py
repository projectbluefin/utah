"""scripts/check-image-parity.py must find a real gap and must not invent one.

Everything here is offline: the registry is a dict of canned responses, and
the image inventory is a text file. What is under test is the reading of the
manifest, the comparison, and the exit code.
"""

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses resolve annotations through sys.modules
    spec.loader.exec_module(module)
    return module


parity = load("check-image-parity")

INVENTORY = {"glibc-all-langpacks": "2.43-8.fc44", "nautilus": "50.2.2-2.fc44", "kmod-zfs": "2.3-1"}
ANNOTATION = json.dumps({"version": 2, "packages": INVENTORY})


def registry(manifest_by_url):
    """A fetch() over canned responses; records what was asked for."""
    calls = []

    def fetch(url, headers):
        calls.append((url, headers))
        if url.startswith("https://ghcr.io/token"):
            return json.dumps({"token": "t0k"}).encode()
        return json.dumps(manifest_by_url[url]).encode()

    fetch.calls = calls
    return fetch


class ImageRefTests(unittest.TestCase):
    def test_tag_digest_and_default_tag(self):
        self.assertEqual(parity.parse_image_ref("ghcr.io/ublue-os/bluefin:stable"), ("ghcr.io", "ublue-os/bluefin", "stable"))
        self.assertEqual(parity.parse_image_ref("ghcr.io/o/r@sha256:ab"), ("ghcr.io", "o/r", "sha256:ab"))
        self.assertEqual(parity.parse_image_ref("quay.io/o/r"), ("quay.io", "o/r", "latest"))

    def test_a_reference_without_a_registry_is_rejected(self):
        with self.assertRaises(ValueError):
            parity.parse_image_ref("ublue-os/bluefin:stable")


class ManifestTests(unittest.TestCase):
    base = "https://ghcr.io/v2/ublue-os/bluefin/manifests/"

    def test_reads_the_inventory_off_a_single_manifest(self):
        fetch = registry({self.base + "stable": {"annotations": {parity.RECHUNK_ANNOTATION: ANNOTATION}}})
        self.assertEqual(parity.bluefin_inventory("ghcr.io/ublue-os/bluefin:stable", fetch), INVENTORY)
        # ghcr.io wants a pull token even for a public image.
        self.assertEqual(fetch.calls[-1][1]["Authorization"], "Bearer t0k")

    def test_follows_a_multi_arch_index_to_the_amd64_manifest(self):
        index = {
            "manifests": [
                {"digest": "sha256:arm", "platform": {"architecture": "arm64", "os": "linux"}},
                {"digest": "sha256:amd", "platform": {"architecture": "amd64", "os": "linux"}},
            ]
        }
        fetch = registry(
            {
                self.base + "stable": index,
                self.base + "sha256:amd": {"annotations": {parity.RECHUNK_ANNOTATION: ANNOTATION}},
            }
        )
        self.assertEqual(parity.bluefin_inventory("ghcr.io/ublue-os/bluefin:stable", fetch), INVENTORY)

    def test_an_image_without_the_annotation_is_an_error_not_an_empty_list(self):
        fetch = registry({self.base + "stable": {"annotations": {}}})
        with self.assertRaises(ValueError):
            parity.bluefin_inventory("ghcr.io/ublue-os/bluefin:stable", fetch)


class ComparisonTests(unittest.TestCase):
    def test_a_missing_name_is_a_gap_unless_explained(self):
        result = parity.compare(INVENTORY, {"nautilus": "51"}, set(), [])
        self.assertEqual(result.missing, ["glibc-all-langpacks", "kmod-zfs"])
        self.assertEqual(result.explained, [])
        self.assertEqual(result.extra, [])

    def test_unavailable_and_exceptions_explain_and_the_reason_travels(self):
        exceptions = [parity.Exception_("kmod-*", "no akmods for this kernel")]
        result = parity.compare(INVENTORY, {}, {"nautilus"}, exceptions)
        self.assertEqual(result.missing, ["glibc-all-langpacks"])
        self.assertEqual(
            result.explained,
            [("kmod-zfs", "no akmods for this kernel"), ("nautilus", "documented under [unavailable] in utah.toml")],
        )

    def test_names_only_here_are_reported_not_counted_as_gaps(self):
        result = parity.compare(INVENTORY, {**INVENTORY, "hummingbird-release": "1"}, set(), [])
        self.assertEqual(result.missing, [])
        self.assertEqual(result.extra, ["hummingbird-release"])

    def test_rpm_list_accepts_bare_names_and_skips_gpg_pubkey(self):
        parsed = parity.parse_rpm_list("nautilus\t51-1\ngpg-pubkey\t1-2\nglibc\n")
        self.assertEqual(parsed, {"nautilus": "51-1", "glibc": ""})

    def test_a_baseline_separates_known_gaps_from_new_ones_and_reports_closed_ones(self):
        baseline = {"glibc-all-langpacks", "nautilus"}
        result = parity.compare(INVENTORY, {"nautilus": "51"}, set(), [], baseline)
        self.assertEqual(result.known, ["glibc-all-langpacks"])
        self.assertEqual(result.new, ["kmod-zfs"])
        self.assertEqual(result.closed, ["nautilus"])
        self.assertEqual(result.missing, ["glibc-all-langpacks", "kmod-zfs"])

    def test_baseline_file_ignores_comments_and_blank_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "baseline.txt"
            path.write_text("# header\n\nnautilus  # trailing\n  7zip\n")
            self.assertEqual(parity.load_baseline(path), {"nautilus", "7zip"})

    def test_the_shipped_baseline_is_sorted_within_each_group_and_has_no_duplicates(self):
        names = [l.split("#")[0].strip() for l in (ROOT / "packages/parity-baseline.txt").read_text().splitlines()]
        names = [n for n in names if n]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(len(names), 969)

    def test_the_shipped_exceptions_file_parses_and_every_entry_has_a_reason(self):
        for exc in parity.load_exceptions(ROOT / "packages/parity-exceptions.toml"):
            self.assertTrue(exc.pattern and exc.reason)


class ExitCodeTests(unittest.TestCase):
    def run_main(self, *extra, rpm_names=("nautilus",), baseline=""):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "bluefin.json").write_text(json.dumps(INVENTORY))
            (tmp / "rpms.txt").write_text("\n".join(rpm_names) + "\n")
            (tmp / "utah.toml").write_text('[unavailable]\npackages = ["kmod-zfs"]\n')
            (tmp / "none.toml").write_text("")
            (tmp / "baseline.txt").write_text(baseline)
            code = parity.main(
                [
                    "--bluefin-inventory", str(tmp / "bluefin.json"),
                    "--rpm-list", str(tmp / "rpms.txt"),
                    "--overlay", str(tmp / "utah.toml"),
                    "--exceptions", str(tmp / "none.toml"),
                    "--baseline", str(tmp / "baseline.txt"),
                    "--report", str(tmp / "report.txt"),
                    *extra,
                ]
            )
            return code, (tmp / "report.txt").read_text()

    def test_report_only_never_fails_but_names_the_gap(self):
        code, report = self.run_main()
        self.assertEqual(code, 0)
        self.assertIn("NEW -- in Bluefin's image", report)
        self.assertIn("glibc-all-langpacks", report)

    def test_strict_fails_on_a_new_gap_and_passes_without_one(self):
        self.assertEqual(self.run_main("--strict")[0], 1)
        code, report = self.run_main("--strict", rpm_names=("nautilus", "glibc-all-langpacks"))
        self.assertEqual(code, 0)
        self.assertNotIn("NEW --", report)
        self.assertIn("kmod-zfs: documented under [unavailable]", report)

    def test_strict_tolerates_a_gap_the_baseline_already_records(self):
        code, report = self.run_main("--strict", baseline="glibc-all-langpacks\n")
        self.assertEqual(code, 0)
        self.assertIn("known gap (baseline):", report)
        self.assertNotIn("NEW --", report)

    def test_an_unreadable_registry_is_not_evidence_about_the_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = ["--bluefin-image", "ghcr.io/nowhere/nothing:tag", "--rpm-list", str(Path(tmp) / "x")]
            Path(tmp, "x").write_text("nautilus\n")
            fetch_failed = parity.main([*args, "--exceptions", str(Path(tmp) / "none")])
            self.assertEqual(fetch_failed, 0)
            self.assertEqual(parity.main([*args, "--strict", "--exceptions", str(Path(tmp) / "none")]), 1)


if __name__ == "__main__":
    unittest.main()
