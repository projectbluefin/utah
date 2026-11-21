"""The experimental image-based installer track (docs/adr/0001, iso/raw/).

Covers the medium's boot configuration and shrink plan (iso/raw/medium.py),
the harness's per-stage results (iso/raw/raw_e2e.py), the size measurement
(iso/raw/measure_medium.py), the comparison with the ISO/fisherman luks job
(iso/raw/compare_paths.py), and the promise that the track never gates
anything: image-installer.yml is not a required check, not in
promote-to-testing's needs, and publishes nothing.
"""

import importlib.util
import json
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "iso/raw"
WORKFLOW = ROOT / ".github/workflows/image-installer.yml"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


medium = load("raw_medium", RAW / "medium.py")
raw_e2e = load("raw_e2e", RAW / "raw_e2e.py")
measure_medium = load("measure_medium", RAW / "measure_medium.py")
compare = load("compare_paths", RAW / "compare_paths.py")

ENTRY = """title Fedora Linux 44 (Utah) (ostree:0)
version 1
options root=UUID=8f3e rw boot=UUID=1b2c rhgb quiet ostree=/ostree/boot.1/default/abc/0
linux /ostree/default-abc/vmlinuz-7.0
initrd /ostree/default-abc/initramfs-7.0.img
"""


class LiveEntryTests(unittest.TestCase):
    def test_live_entry_boots_read_only_with_the_live_karg_first(self):
        live = medium.live_entry(ENTRY, ["utah.live.debug"])
        self.assertIn("title Utah Live (try or install)\n", live)
        self.assertIn("version 1001\n", live)
        options = next(line for line in live.splitlines() if line.startswith("options "))
        words = options.split()[1:]
        self.assertNotIn("rw", words)
        self.assertEqual(words[-3:], ["ro", "utah.live", "utah.live.debug"])
        self.assertIn("rhgb", words)
        self.assertIn("linux /ostree/default-abc/vmlinuz-7.0\n", live)

    def test_live_entry_never_duplicates_the_karg(self):
        twice = medium.live_entry(medium.live_entry(ENTRY))
        self.assertEqual(twice.count("utah.live"), 1)

    def test_an_entry_without_options_is_rejected(self):
        with self.assertRaises(ValueError):
            medium.live_entry("title x\nversion 1\n")


class GrubenvTests(unittest.TestCase):
    def test_grubenv_is_one_padded_block_defaulting_to_the_live_entry(self):
        current = "# GRUB Environment Block\nboot_success=1\nsaved_entry=old\n" + "#" * 900
        env = medium.grubenv(current, "utah-live")
        self.assertEqual(len(env), medium.GRUBENV_SIZE)
        self.assertTrue(env.startswith(medium.GRUBENV_HEADER))
        body = [line for line in env.split("\n") if line and not line.startswith("#")]
        self.assertEqual(body, ["boot_success=1", "saved_entry=utah-live"])


TABLE = {"partitiontable": {"label": "gpt", "sectorsize": 512, "partitions": [
    {"node": "/dev/loop0p1", "start": 2048, "size": 2048},
    {"node": "/dev/loop0p2", "start": 4096, "size": 1048576},
    {"node": "/dev/loop0p3", "start": 1052672, "size": 2097152},
    {"node": "/dev/loop0p4", "start": 3149824, "size": 47181790},
]}}


class ShrinkPlanTests(unittest.TestCase):
    def test_root_is_cut_to_the_filesystem_rounded_to_a_mebibyte(self):
        number, sectors, image = medium.shrink_plan(TABLE, 7 * 2**30 + 1)
        self.assertEqual(number, 4)
        self.assertEqual(sectors % 2048, 0)
        self.assertGreaterEqual(sectors * 512, 7 * 2**30 + 1)
        self.assertLess(sectors * 512, 7 * 2**30 + 2**20 + 1)
        self.assertEqual(image, (3149824 + sectors) * 512 + medium.GPT_TAIL_BYTES)

    def test_a_filesystem_larger_than_its_partition_is_an_error(self):
        with self.assertRaises(ValueError):
            medium.shrink_plan(TABLE, 47181790 * 512 + 2**20)

    @unittest.skipUnless(shutil.which("sfdisk"), "sfdisk not installed")
    def test_the_cut_medium_keeps_a_valid_gpt(self):
        """Run 44874576635: truncating without rewriting the GPT left no partitions."""
        import subprocess
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp, "m.raw")
            with open(raw, "wb") as f:
                f.truncate(24 * 2**30)
            layout = ('label: gpt\nsize=1MiB, type=21686148-6449-6E6F-744E-656564454649\n'
                      'size=512MiB, type=C12A7328-F81F-11D2-BA4B-00A0C93EC93B\n'
                      'size=1GiB, type=0FC63DAF-8483-4772-8E79-3D69D8477DE4\n'
                      'type=4F68BCE3-E8CD-4DB1-96E7-FBCAF984B709, name="root"\n')
            run = lambda *a, **k: subprocess.run(["sfdisk", *a], check=True, capture_output=True, text=True, **k).stdout  # noqa: E731
            run("-q", str(raw), input=layout)
            number, sectors, size = medium.shrink_plan(json.loads(run("--json", str(raw))), 8 * 2**30)
            new = medium.shrunk_dump(run("--dump", str(raw)), number, sectors)
            with open(raw, "r+b") as f:
                f.truncate(size)
            run("-q", "--no-reread", "--wipe", "never", "--wipe-partitions", "never", str(raw), input=new)
            table = json.loads(run("--json", str(raw)))["partitiontable"]
            self.assertEqual(table["label"], "gpt")
            self.assertEqual(int(table["partitions"][-1]["size"]), sectors)
            self.assertLess(int(table["lastlba"]), size // 512)

    def test_shrunk_dump_needs_the_partition(self):
        with self.assertRaises(ValueError):
            medium.shrunk_dump("label: gpt\nx1 : start= 1, size= 2, type=a\n", 4, 8)

    def test_the_build_script_and_the_shrink_script_use_these_helpers(self):
        build = (RAW / "build-raw-image.sh").read_text()
        self.assertIn('medium.py" live-entry', build)
        self.assertIn('medium.py" grubenv', build)
        shrink = (RAW / "shrink-raw-image.sh").read_text()
        self.assertIn('medium.py" shrink-plan', shrink)
        self.assertIn('medium.py" shrunk-dump', shrink)
        self.assertIn("--wipe-partitions never", shrink)
        self.assertNotIn("--relocate", shrink)


class ResultsTests(unittest.TestCase):
    def test_stages_pass_fail_and_not_run(self):
        clock = iter(range(0, 1000, 10))
        results = raw_e2e.Results("gui", clock=lambda: next(clock))
        results.begin("live boot")
        results.begin("install")
        results.finish(RuntimeError("installer failed"))
        out = results.as_dict({"x": 1})
        self.assertEqual(out["result"], "fail")
        self.assertEqual(out["error"], "installer failed")
        status = {s["stage"]: s["status"] for s in out["stages"]}
        self.assertEqual(status["live boot"], "pass")
        self.assertEqual(status["install"], "fail")
        self.assertEqual(status["bootc rollback"], "not run")
        self.assertEqual([s["stage"] for s in out["stages"]], list(raw_e2e.STAGES))

    def test_all_stages_passed_is_a_pass(self):
        results = raw_e2e.Results("script")
        for stage in raw_e2e.STAGES:
            results.begin(stage)
        results.finish()
        self.assertEqual(results.as_dict()["result"], "pass")

    def test_unknown_stage_names_are_rejected(self):
        with self.assertRaises(ValueError):
            raw_e2e.Results("gui").begin("desktop")

    def test_failed_units_are_read_from_systemctl(self):
        text = "\u25cf bootc-generic-growpart.service loaded failed failed Grow root\nfoo.mount loaded failed failed x\n"
        self.assertEqual(raw_e2e.parse_failed_units(text), ["bootc-generic-growpart.service", "foo.mount"])
        self.assertEqual(raw_e2e.parse_failed_units(""), [])


LUKS_LOG = """\ufeff2026-11-20T04:35:36.3414740Z ISO ready: /home/runner/work/utah/utah/output/utah-live.iso (5.3G)
2026-11-20T04:36:01.6028470Z === Phase 1/7: boot the live ISO ===
2026-11-20T04:36:34.3891307Z === Phase 2/7: prove the live session reached a desktop ===
2026-11-20T04:36:48.1857085Z === Phase 3/7: install onto an encrypted disk ===
2026-11-20T04:44:43.1516846Z === Phase 4/7: boot the installed disk ===
"""
FAILED_LOG = """2026-11-21T05:10:00.4042580Z ISO ready: /x/utah-live.iso (5.3G)
2026-11-21T05:10:27.2196484Z === Phase 1/7: boot the live ISO ===
2026-11-21T05:10:58.4275890Z === Phase 2/7: prove the live session reached a desktop ===
2026-11-21T05:11:12.3095662Z === Phase 3/7: install onto an encrypted disk ===
2026-11-21T05:20:37.0384039Z ##[error]Process completed with exit code 13.
"""


class FishermanSideTests(unittest.TestCase):
    def test_install_time_is_phase_three_to_phase_four(self):
        parsed = compare.parse_luks_log(LUKS_LOG)
        self.assertEqual(parsed["iso_size"], "5.3G")
        self.assertEqual(compare.luks_install_seconds(parsed), (475, "pass"))

    def test_a_failed_install_reports_where_and_after_how_long(self):
        parsed = compare.parse_luks_log(FAILED_LOG)
        self.assertEqual(parsed["error"][1], 13)
        self.assertEqual(compare.luks_install_seconds(parsed), (565, "fail"))
        status = [s["status"] for s in compare.luks_stages(parsed, "failure")]
        self.assertEqual(status, ["pass", "pass", "fail", "not run", "not run", "not run", "not run"])

    def test_the_luks_job_is_matched_by_image_and_digest(self):
        digest = "sha256:" + "1c7956e2" * 8
        jobs = [{"name": f"luks (utah-gaming, {digest}, ghcr.io/x...)"},
                {"name": f"luks (utah, sha256:{'0' * 64}, ghcr.io/x...)"},
                {"name": f"luks (utah, {digest}, ghcr.io/proj..."}]
        self.assertIs(compare.match_luks_job(jobs, "utah", digest), jobs[2])
        self.assertIsNone(compare.match_luks_job(jobs, "utah-nvidia", digest))

    def test_fetch_reads_the_newest_completed_run_with_that_job(self):
        digest = "sha256:" + "ab" * 32
        job = {"id": 7, "name": f"luks (utah, {digest}, r)", "conclusion": "success", "html_url": "j",
               "steps": [{"name": compare.ISO_STEP, "started_at": "2026-11-20T04:26:15Z",
                          "completed_at": "2026-11-20T04:35:36Z"}]}
        calls = []

        def api(path, raw=False):
            calls.append(path)
            if path.endswith("runs?per_page=60"):
                return {"workflow_runs": [{"id": 3, "status": "in_progress"},
                                          {"id": 2, "status": "completed", "html_url": "r2"},
                                          {"id": 1, "status": "completed", "html_url": "r1"}]}
            if "/runs/2/jobs" in path:
                return {"jobs": [{"id": 5, "name": "resolve"}]}
            if "/runs/1/jobs" in path:
                return {"jobs": [job]}
            if path.endswith("/jobs/7/logs"):
                return LUKS_LOG
            if "/runs/1/artifacts" in path:
                return {"artifacts": [{"name": "production-iso-utah", "size_in_bytes": 5556307570}]}
            raise AssertionError(path)

        got = compare.fetch_fisherman("o/r", "utah", digest, api=api)
        self.assertEqual(got["run"], 1)
        self.assertEqual(got["install_seconds"], 475)
        self.assertEqual(got["iso_build_seconds"], 561)
        self.assertEqual(got["production_iso_artifact_bytes"], 5556307570)
        self.assertFalse(any("/runs/3/" in c for c in calls))


class ComparisonTests(unittest.TestCase):
    def test_image_side_and_markdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            ev, rev = Path(tmp, "ev"), Path(tmp, "rev")
            ev.mkdir()
            rev.mkdir()
            results = raw_e2e.Results("gui")
            for stage in raw_e2e.STAGES:
                results.begin(stage)
            results.boot("live", "Enforcing", [])
            results.boot("first boot", "Enforcing", ["x.service"])
            results.finish()
            (ev / "results.json").write_text(json.dumps(results.as_dict({"gui-install-seconds-after-confirm": 145})))
            (ev / "utah-install.log").write_text("Block copy finished in 138s\nUtah installed to /dev/vdb in 144s\n")
            (rev / "medium.json").write_text('{"raw_apparent_bytes": 1, "raw_allocated_bytes": 1, "bootc_install_seconds": 147}')
            size = Path(tmp, "size.json")
            size.write_text(json.dumps({"label": "shrunk", "apparent_bytes": 8 * 2**30, "allocated_bytes": 7 * 2**30,
                                        "formats": {"zstd-3": {"bytes": 3 * 2**30, "compress_seconds": 60.0,
                                                               "decompress_seconds": 20.0}}}))
            img = compare.image_metrics(ev, rev, [size], 300)
        self.assertEqual(img["install_seconds"], 144)
        self.assertEqual(img["block_copy_seconds"], 138)
        self.assertEqual(img["bootc_install_seconds"], 147)
        md = compare.render({"image": "utah", "digest": "sha256:" + "a" * 64, "image_based": img,
                             "fisherman": None, "references": {"GNOME OS nightly": 3 * 2**30}})
        self.assertIn("| Install time | 2 min 24 s install.sh", md)
        self.assertIn("first boot: x.service", md)
        self.assertIn("| shrunk | 8.00 GiB | 7.00 GiB | 3.00 GiB", md)
        self.assertIn("no Post-Testing E2E `luks` job found", md)
        self.assertIn("GNOME OS nightly ISO 3.00 GiB", md)


@unittest.skipUnless(shutil.which("zstd"), "zstd not installed")
class MeasureTests(unittest.TestCase):
    def test_sizes_and_pack_unpack_times_are_recorded_and_packs_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp, "medium.raw")
            with open(raw, "wb") as f:
                f.write(b"utah" * 4096)
                f.truncate(64 * 2**20)
            got = measure_medium.measure(raw, ["zstd-3"], "t")
            self.assertEqual(got["apparent_bytes"], 64 * 2**20)
            self.assertLess(got["allocated_bytes"], got["apparent_bytes"])
            self.assertLess(got["formats"]["zstd-3"]["bytes"], 2**20)
            self.assertEqual(sorted(p.name for p in Path(tmp).iterdir()), ["medium.raw"])
            with self.assertRaises(ValueError):
                measure_medium.measure(raw, ["gzip"])


class NonBlockingTrackTests(unittest.TestCase):
    workflow = yaml.safe_load(WORKFLOW.read_text())

    def test_the_track_runs_nightly_on_dispatch_and_on_its_own_changes(self):
        on = self.workflow[True]
        self.assertIn("workflow_dispatch", on)
        self.assertTrue(on["schedule"])
        self.assertIn("iso/raw/**", on["pull_request"]["paths"])
        self.assertNotIn("push", on)
        self.assertNotIn("merge_group", on)

    def test_the_track_has_no_write_permission(self):
        self.assertEqual(self.workflow["permissions"], {})
        for name, job in self.workflow["jobs"].items():
            for scope, level in (job.get("permissions") or {}).items():
                self.assertEqual(level, "read", f"{name}: {scope}")

    def test_nothing_gates_on_the_track(self):
        e2e = yaml.safe_load((ROOT / ".github/workflows/post-testing-e2e.yml").read_text())
        needs = e2e["jobs"]["promote-to-testing"]["needs"]
        self.assertFalse([n for n in needs if "image" in n or "raw" in n])
        for other in (ROOT / ".github/workflows").glob("*.yml"):
            if other != WORKFLOW:
                self.assertNotIn("image-installer", other.read_text(), other.name)
        name = self.workflow["jobs"]["e2e"]["name"]
        self.assertNotIn("Check all testing builds successful", name)
        self.assertNotEqual(name, "contract")

    def test_os_installer_is_pinned_by_commit(self):
        containerfile = (RAW / "Containerfile").read_text()
        self.assertRegex(containerfile, r"(?m)^ARG OSI_REF=[0-9a-f]{40}$")
        self.assertNotRegex(containerfile, r"\bgit\s+clone\b")
        self.assertIn('test "$(git -C /src rev-parse HEAD)" = "${OSI_REF}"', containerfile)
        self.assertRegex(containerfile, r"(?m)^ARG BUILDER_IMAGE=\S+@sha256:[0-9a-f]{64}$")

    def test_the_medium_is_never_uploaded(self):
        upload = next(s for s in self.workflow["jobs"]["e2e"]["steps"] if "upload-artifact" in s.get("uses", ""))
        paths = upload["with"]["path"]
        self.assertNotRegex(paths, r"medium\.raw|\.qcow2|id_ed25519")


class OfflineFlatpakTests(unittest.TestCase):
    """ADR-0001 decision 6: Flatpaks on the medium's /var, never in an image."""

    def test_a_removed_app_must_stay_removed_and_the_rest_stay(self):
        baked = ["org.gnome.Calculator", "org.mozilla.firefox", "com.mitchellh.ghostty"]
        self.assertEqual(raw_e2e.flatpak_problems(baked, baked), [])
        self.assertEqual(raw_e2e.flatpak_problems(baked, baked[1:], "org.gnome.Calculator"), [])
        self.assertEqual(raw_e2e.flatpak_problems(baked, baked, "org.gnome.Calculator"),
                         ["org.gnome.Calculator came back after it was removed"])
        self.assertEqual(raw_e2e.flatpak_problems(baked, baked[:1]),
                         ["missing com.mitchellh.ghostty", "missing org.mozilla.firefox"])

    def test_results_carry_the_flatpak_checks(self):
        results = raw_e2e.Results("gui")
        results.flatpak("first boot", ["a", "b"], [])
        self.assertEqual(results.as_dict()["flatpaks"], {"first boot": {"apps": 2, "problems": []}})

    def test_no_image_layer_installs_flatpaks(self):
        containerfile = (RAW / "Containerfile").read_text()
        self.assertNotRegex(containerfile, r"(?i)flatpak")
        for path in (ROOT / "Containerfile", ROOT / "Containerfile.kernel"):
            self.assertNotRegex(path.read_text(), r"flatpak (install|preinstall)")

    def test_the_medium_bakes_them_into_the_stateroot_var_with_labels(self):
        bake = (RAW / "bake-flatpaks.sh").read_text()
        self.assertIn('-v "${stateroot}/var/lib/flatpak:/var/lib/flatpak"', bake)
        self.assertIn("flatpak preinstall --system", bake)
        self.assertIn("setfiles -F -r /stateroot", bake)
        self.assertNotRegex(bake, r"podman (build|commit|push)")

    def test_the_workflow_asserts_no_image_flatpaks_and_bakes_before_shrinking(self):
        steps = yaml.safe_load(WORKFLOW.read_text())["jobs"]["e2e"]["steps"]
        names = [s.get("name", "") for s in steps]
        assert_step = steps[names.index("Assert the published image and the live layer carry no Flatpaks")]
        self.assertIn("/var/lib/flatpak", assert_step["run"])
        bake = steps[names.index("Bake the Flatpaks into the medium and shrink it")]["run"]
        self.assertLess(bake.index("bake-flatpaks.sh"), bake.rindex('shrink-raw-image.sh localhost/utah-raw:ci "$WORK/medium.raw"'))
        e2e = steps[names.index("End-to-end with screenshots")]["run"]
        self.assertIn("--flatpaks", e2e)

    def test_flatpaks_are_on_by_default_and_a_dispatch_can_turn_them_off(self):
        workflow = yaml.safe_load(WORKFLOW.read_text())
        flag = workflow[True]["workflow_dispatch"]["inputs"]["flatpaks"]
        self.assertEqual((flag["type"], flag["default"]), ("boolean", True))
        # A bare inputs.flatpaks is null on schedule and pull_request, which
        # would turn the bake off there.
        self.assertEqual(workflow["jobs"]["e2e"]["env"]["FLATPAKS"],
                         "${{ github.event_name != 'workflow_dispatch' || inputs.flatpaks }}")

    def test_comparison_reports_baked_flatpaks_and_removal(self):
        cell = compare.flatpak_cell({"baked": {"apps": 20, "bytes": 3 * 2**30, "seconds": 400},
                                     "checks": {"first boot": {"problems": []},
                                                "after bootc switch": {"problems": []},
                                                "after bootc rollback": {"problems": []}}})
        self.assertIn("20 apps, 3.00 GiB", cell)
        self.assertIn("stayed removed through switch and rollback", cell)
        self.assertEqual(compare.flatpak_cell({}), "none baked")


if __name__ == "__main__":
    unittest.main()
