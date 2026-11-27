"""Image-installer track tests: gnome-setup install medium checks.

Split from the former single tests/test_image_installer.py; see docs/adr/0001 and iso/raw/.
"""

import importlib.util
import subprocess
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


class GnomeSetupTests(unittest.TestCase):
    """gnome-setup drives systemd-sysinstall on the live medium."""

    workflow = yaml.safe_load(WORKFLOW.read_text())

    def test_the_live_karg_starts_the_installer(self):
        build = (RAW / "build-raw-image.sh").read_text()
        self.assertIn("utah.live.installer=gnome-setup", build)
        for unit in ("utah-sysinstall.socket", "utah-cryptenroll.socket"):
            text = (RAW / "src/usr/lib/systemd/system" / unit).read_text()
            self.assertIn("ConditionKernelCommandLine=utah.live.installer=gnome-setup", text)

    def test_sysinstall_leaves_the_copied_boot_chain_alone(self):
        service = (RAW / "src/usr/lib/systemd/system/utah-sysinstall@.service").read_text()
        self.assertIn("--install-kernel=no --install-boot-loader=no", service)
        self.assertIn("SYSTEMD_REPART_PATH=/usr/libexec/utah-sysinstall/systemd-repart", service)

    def test_the_post_install_unit_is_the_one_gnome_setup_starts(self):
        pages = (RAW / "src/usr/share/utah/gnome-setup/pages.conf").read_text()
        self.assertIn("post-install-unit=utah-sysinstall-post@.service", pages)
        self.assertTrue((RAW / "src/usr/lib/systemd/system/utah-sysinstall-post@.service").exists())
        rules = (RAW / "src/usr/share/polkit-1/rules.d/50-utah-live-gnome-setup.rules").read_text()
        self.assertIn("utah-sysinstall-post@", rules)
        containerfile = (RAW / "Containerfile").read_text()
        self.assertIn("ln -s /run/utah-live/repart.d /out/usr/lib/gnome-setup/definitions", containerfile)
        self.assertIn("/usr/share/utah/gnome-setup/pages.conf /usr/lib/gnome-setup/pages.conf", containerfile)

    def test_the_live_user_is_authorized_by_name_not_by_a_marker_polkit_cannot_read(self):
        # gnome-setup's rule cats /run/gnomeos/is-live from polkitd, which
        # SELinux denies (policykit_t, var_run_t) on every check
        # (run 45918946265); the live user is granted by name instead.
        setup = (RAW / "src/usr/libexec/utah-live-setup").read_text()
        code = [line for line in setup.splitlines() if not line.lstrip().startswith("#")]
        self.assertFalse([line for line in code if "/run/gnomeos" in line])
        rules = (RAW / "src/usr/share/polkit-1/rules.d/50-utah-live-gnome-setup.rules").read_text()
        self.assertIn('subject.user != "liveuser"', rules)
        for action in ("io.systemd.sysinstall.Run", "io.systemd.sysinstall.ListCandidateDevices",
                       "io.systemd.cryptenroll.enroll", "io.systemd.cryptenroll.list-slots"):
            self.assertIn(f'action.id == "{action}"', rules)
        self.assertNotIn("polkit.spawn", rules)

    def test_without_a_tpm_the_user_picks_a_passphrase_next_to_the_recovery_key(self):
        pages = (RAW / "src/usr/share/utah/gnome-setup/pages.conf").read_text()
        flow = next(line for line in pages.splitlines() if line.startswith("live-initial-setup="))
        steps = flow.split("=", 1)[1].split(";")
        self.assertLess(steps.index("installer"), steps.index("disk-passphrase"))
        self.assertLess(steps.index("disk-passphrase"), steps.index("recovery-key"))
        inputs = self.workflow[True]["workflow_dispatch"]["inputs"]
        self.assertEqual(inputs["tpm"]["type"], "boolean")
        self.assertTrue(inputs["tpm"]["default"])
        self.assertIn('[[ "$TPM" != true ]] || tpm=(--tpm)', WORKFLOW.read_text())

    def test_the_live_session_names_the_product_as_dakota_does(self):
        setup = (RAW / "src/usr/libexec/utah-live-setup").read_text()
        self.assertNotIn("Install Utah", setup)
        self.assertIn("\nName=Install Bluefin\n", setup)
        # The sed line itself, over a desktop file shaped like upstream's.
        line = setup[setup.index("sed -e '/^Name"):setup.index("# end of desktop entry")]
        with tempfile.TemporaryDirectory() as d:
            src, dst = Path(d) / "src.desktop", Path(d) / "dst.desktop"
            src.write_text("[Desktop Entry]\nName[de]=Einrichtung\nName=Setup\nExec=gnome-setup\n")
            line = line.replace("/usr/share/utah/gnome-setup/org.gnome.Setup.desktop", str(src))
            line = line.replace("/usr/local/share/applications/org.gnome.Setup.desktop", str(dst))
            subprocess.run(["bash", "-euc", line], check=True)
            self.assertEqual(dst.read_text(), "[Desktop Entry]\nName=Install Bluefin\n"
                             "Comment=Install Bluefin on this computer\nExec=gnome-setup\n")

    def test_installed_systems_do_not_offer_gnome_setup(self):
        containerfile = (RAW / "Containerfile").read_text()
        self.assertIn("mv /out/usr/share/applications/org.gnome.Setup.desktop", containerfile)
        self.assertIn("rm -rf /out/usr/lib/systemd/user", containerfile)

    def test_results_record_the_installer_and_encryption(self):
        results = raw_e2e.Results("gui")
        results.installer = "gnome-setup"
        out = results.as_dict()
        self.assertEqual(out["installer"], "gnome-setup")
        self.assertIn("encryption", out)

    @staticmethod
    def showing(*names, role="label"):
        return [{"path": f"0.{i}", "role": role, "name": name, "text": "",
                 "ext": [0, 0, 10, 10], "states": ["sensitive", "showing", "visible"]}
                for i, name in enumerate(names)]

    def test_installer_pages_classify_from_gnome_setup_text(self):
        # Headings and response labels of gnome-setup 80825ae's dialogs
        # (src/page/installer/disk/confirm.rs) and pages; the dialogs sit over
        # the still-showing disk page.
        disk = self.showing("Select a Disk", "Unknown Disk 51.5\u00a0GB", raw_e2e.DISK_EMPTY_TEXT)
        page = raw_e2e.Page
        for nodes, expected in (
            (disk, page.DISK),
            (disk + self.showing("Begin Installation?") + self.showing("Cancel", "Install", role="button"),
             page.CONFIRM),
            (self.showing("Existing Data Found", "Install to Free Space"), page.DATA_FOUND),
            (disk + self.showing("Begin Installation?") + self.showing("Cancel", "Erase and Install", role="button"),
             page.ERASE),
            (disk + self.showing("Insufficient Free Space") + self.showing("Erase and Install (3)", role="button"),
             page.ERASE),
            (disk + self.showing("Disk is Full") + self.showing("Erase and Install", role="button"), page.ERASE),
        ):
            with self.subTest(expected=expected, last=nodes[-1]["name"]):
                self.assertIs(raw_e2e.classify_setup_page(nodes), expected)

    def test_existing_data_on_the_blank_target_is_a_failure(self):
        # install.qcow2 is created fresh: the data page, any erase confirmation,
        # or a target row saying it holds data means empty-disk detection broke.
        page = raw_e2e.Page
        for p in (page.DATA_FOUND, page.ERASE):
            self.assertEqual(raw_e2e.blank_target_problem(p), raw_e2e.BLANK_TARGET_DATA)
        self.assertIn("existing data on the blank target disk", raw_e2e.BLANK_TARGET_DATA)
        data_row = self.showing("Unknown Disk 51.5\u00a0GB", raw_e2e.DISK_DATA_TEXT)
        self.assertEqual(raw_e2e.blank_target_problem(page.DISK, data_row), raw_e2e.BLANK_TARGET_DATA)
        for p in (page.DISK, page.CONFIRM, page.PROGRESS, page.UNKNOWN):
            self.assertIsNone(raw_e2e.blank_target_problem(p))
        empty_row = self.showing("Unknown Disk 51.5\u00a0GB", raw_e2e.DISK_EMPTY_TEXT)
        self.assertIsNone(raw_e2e.blank_target_problem(page.DISK, empty_row))

    def test_the_target_row_status_is_read_from_its_own_subtree(self):
        # The live medium's row says it holds data; only the target's row counts.
        nodes = [
            {"path": "1.0", "role": "table row", "name": "Unknown Disk 51.5\u00a0GB", "text": "",
             "ext": [0, 0, 100, 20], "states": ["showing"]},
            {"path": "1.0.1", "role": "label", "name": raw_e2e.DISK_EMPTY_TEXT, "text": "",
             "ext": [0, 0, 100, 20], "states": ["showing"]},
            {"path": "1.1", "role": "table row", "name": "Unknown Disk 9.7\u00a0GB", "text": "",
             "ext": [0, 30, 100, 20], "states": ["showing"]},
            {"path": "1.1.1", "role": "label", "name": raw_e2e.DISK_DATA_TEXT, "text": "",
             "ext": [0, 30, 100, 20], "states": ["showing"]},
        ]
        row = raw_e2e.row_containing(nodes, raw_e2e.TARGET_SIZE_TEXT)
        shown = raw_e2e.subtree(nodes, row)
        self.assertEqual([n["path"] for n in shown], ["1.0", "1.0.1"])
        self.assertIsNone(raw_e2e.blank_target_problem(raw_e2e.Page.DISK, shown))

    def test_recovery_key_read_from_label_shaped_like_a_key(self):
        # Run 44972858422's atspi-timeout-recovery-key.json: the key label's
        # accessible name IS the key, so named(nodes, 'Recovery Key') misses it.
        key = "reclhuld-icvnrelc-kjtnebnh-ucghgbnn-kffbvtuf-gbeuntuv-ukbkrgrk-kvhcerjg"
        nodes = [
            {"path": "0.1", "role": "label", "name": "Back Up Recovery Key",
             "text": "Back Up Recovery Key", "ext": [0, 0, 10, 10], "states": []},
            {"path": "0.2", "role": "label", "name": key, "text": key,
             "ext": [0, 0, 10, 10], "states": ["sensitive", "showing", "visible"]},
        ]
        self.assertIsNone(raw_e2e.named(nodes, "Recovery Key"))
        self.assertEqual(raw_e2e.Harness.recovery_key_text(nodes), key)

    def test_done_predicate_ignores_panel_before_checkbox_tick(self):
        # Run 44972858422's atspi-timeout-recovery-key.json: three nodes share
        # the name 'Done' (sensitive panel, insensitive button, sensitive
        # label). Without a role filter the predicate is already true before
        # 'I backed up my recovery key' is ticked, and act may target the panel.
        panel = {"path": "0.0.0.0.0.0.0", "role": "panel", "name": "Done",
                 "text": "", "ext": [0, 0, 1920, 977],
                 "states": ["sensitive", "showing", "visible"]}
        button = {"path": "0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.1.2",
                  "role": "button", "name": "Done", "text": "",
                  "ext": [909, 699, 102, 44],
                  "states": ["focusable", "showing", "visible"]}
        label = {"path": "0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0.1.2.0",
                 "role": "label", "name": "Done", "text": "Done",
                 "ext": [909, 699, 38, 24],
                 "states": ["sensitive", "showing", "visible"]}
        nodes = [panel, button, label]
        self.assertTrue(raw_e2e.has_name(nodes, "Done", sensitive=True))
        self.assertFalse(raw_e2e.has_name(nodes, "Done", sensitive=True, role="button"))
        self.assertIsNone(raw_e2e.named(nodes, "Done", sensitive=True, role="button"))
        ticked = [dict(button, states=button["states"] + ["sensitive"])]
        self.assertTrue(raw_e2e.has_name(ticked, "Done", sensitive=True, role="button"))
        self.assertEqual(raw_e2e.named(ticked, "Done", sensitive=True, role="button")["role"], "button")

    def test_qr_dialog_done_is_the_dialogs_button(self):
        # Run 44978556102's atspi-timeout-qr-code-dialog-closed.json: with the QR
        # dialog open the page has a sensitive panel 'Done' (the whole window),
        # the still-insensitive main button 'Done' behind the modal, and the
        # dialog's own sensitive button 'Done'. The press must land on the last.
        nodes = [
            {"path": "0", "role": "panel", "name": "Done", "text": "",
             "ext": [0, 0, 1920, 977], "states": ["sensitive", "showing"]},
            {"path": "0.1", "role": "button", "name": "Done", "text": "",
             "ext": [909, 699, 102, 44], "states": ["focusable", "showing"]},
            {"path": "0.2", "role": "dialog", "name": "QR Code", "text": "",
             "ext": [0, 0, 1920, 977], "states": ["sensitive", "showing"]},
            {"path": "0.2.0", "role": "button", "name": "Done", "text": "",
             "ext": [808, 619, 304, 44], "states": ["focusable", "sensitive", "showing"]},
        ]
        self.assertEqual(raw_e2e.named(nodes, "Done")["role"], "panel")  # the old pick
        picked = raw_e2e.named(nodes, "Done", sensitive=True, role="button")
        self.assertEqual(picked["ext"], [808, 619, 304, 44])

    def test_setup_done_press_ignores_panels_and_insensitive_buttons(self):
        from unittest.mock import Mock
        harness = object.__new__(raw_e2e.Harness)
        harness.atspi = Mock(return_value={"how": "action:click"})
        nodes = [
            {"name": "Done", "role": "panel", "states": ["sensitive"]},
            {"name": "Done", "role": "button", "states": []},
            {"name": "Done", "role": "button", "states": ["sensitive"]},
        ]
        harness.setup_press(None, nodes, "Done")
        harness.atspi.assert_called_once_with(None, "act", raw_e2e.GS_APP, "Done", "button")

    def test_timeout_summary_lists_buttons_and_labels(self):
        nodes = [
            {"path": "0.1", "role": "button", "name": "Install",
             "text": "", "ext": [0, 0, 1, 1], "states": ["sensitive", "showing"]},
            {"path": "0.2", "role": "panel", "name": "",
             "text": "", "ext": [0, 0, 1, 1], "states": []},
        ]
        lines = raw_e2e.summarize_nodes(nodes)
        self.assertEqual(len(lines), 1)
        self.assertIn("button", lines[0])
        self.assertIn("'Install'", lines[0])
        self.assertIn("showing=True", lines[0])

    def test_disk_rows_match_despite_glib_nbsp_size(self):
        # glib's format_size separates value and unit with a non-breaking space
        # (CI run 44964323210: 'Unknown Disk 51.5\\xa0GB'); the harness matches
        # with a plain space.
        nodes = [{
            "path": "0.1", "role": "table row",
            "name": "Unknown Disk 51.5\u00a0GB", "text": "",
            "ext": [0, 0, 100, 20], "states": [],
        }]
        self.assertTrue(raw_e2e.has_text(nodes, "51.5 GB"))
        row = raw_e2e.row_containing(nodes, raw_e2e.TARGET_SIZE_TEXT)
        self.assertIsNotNone(row)
        self.assertEqual(row["role"], "table row")


if __name__ == "__main__":
    unittest.main()
