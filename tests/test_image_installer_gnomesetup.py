"""Image-installer track tests: gnome-setup install medium checks.

Split from the former single tests/test_image_installer.py; see docs/adr/0001 and iso/raw/.
"""

import configparser
import importlib.util
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

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


class LiveSessionDiagnosticsTests(unittest.TestCase):
    """A live session that cannot draw the installer is explained, not guessed.

    Run 45927940366 (attempt 1) wedged after the live session drew: the
    session's desktop portal never started, gnome-setup blocked on it and
    never opened a window, and the AT-SPI desktop listed no application at
    all, so the harness only saw "no buttons, check boxes or labels".
    """

    FAILED_PORTAL = ("Id=xdg-desktop-portal.service\nActiveState=failed\nSubState=failed\n"
                     "Result=timeout\n"
                     "Id=xdg-desktop-portal-gnome.service\nActiveState=failed\nSubState=failed\n"
                     "Result=timeout\n")

    def test_a_failed_portal_is_read_from_the_unit_report(self):
        self.assertEqual(raw_e2e.failed_session_units(self.FAILED_PORTAL),
                         ["xdg-desktop-portal.service", "xdg-desktop-portal-gnome.service"])

    def test_a_healthy_or_starting_portal_is_not_a_failure(self):
        healthy = ("Id=xdg-desktop-portal.service\nActiveState=active\nSubState=running\n"
                   "Result=success\n")
        self.assertEqual(raw_e2e.failed_session_units(healthy), [])
        starting = "Id=xdg-desktop-portal.service\nActiveState=activating\nSubState=start\n"
        self.assertEqual(raw_e2e.failed_session_units(starting), [])
        self.assertEqual(raw_e2e.failed_session_units(""), [])

    def test_the_session_state_command_reports_the_portal_the_shell_and_the_coredumps(self):
        command = raw_e2e.session_state_command()
        self.assertIn("xdg-desktop-portal.service", command)
        self.assertIn("at-spi2-registryd.service", command)
        self.assertIn("journalctl --user -b", command)
        self.assertIn("/proc/$p/stack", command)
        self.assertIn("coredumpctl list", command)
        self.assertIn("coredumpctl info --no-pager gnome-shell", command)

    def test_the_session_commands_carry_the_live_session_environment(self):
        # `systemctl --user` without XDG_RUNTIME_DIR answers "Failed to connect
        # to bus" and the report comes back empty, which reads as no failure.
        for command in (raw_e2e.portal_state_command(), raw_e2e.session_state_command()):
            self.assertIn("XDG_RUNTIME_DIR", command)
            self.assertIn("DBUS_SESSION_BUS_ADDRESS", command)

    def test_a_failed_portal_ends_the_wait_with_the_reason(self):
        harness = object.__new__(raw_e2e.Harness)
        harness.portal_failed_at = None
        harness.ssh = lambda *args, **kwargs: self.FAILED_PORTAL
        # The first failure only starts the grace period: the portal can still
        # come back, and the installer can open its window without one.
        self.assertIsNone(harness.live_session_stall())
        harness.portal_failed_at = time.monotonic() - raw_e2e.PORTAL_GRACE - 1
        message = harness.live_session_stall()
        self.assertIn("xdg-desktop-portal.service", message)
        self.assertIn("portal", message)

    def test_a_healthy_portal_never_stalls_the_wait(self):
        harness = object.__new__(raw_e2e.Harness)
        harness.portal_failed_at = None
        harness.ssh = lambda *args, **kwargs: (
            "Id=xdg-desktop-portal.service\nActiveState=active\nSubState=running\nResult=success\n")
        self.assertIsNone(harness.live_session_stall())

    def test_the_atspi_desktop_lists_the_applications_it_has(self):
        harness = object.__new__(raw_e2e.Harness)
        harness.atspi = lambda *args, **kwargs: {"error": "no application matching 'setup'",
                                                  "apps": ["Firefox", "org.gnome.Nautilus"]}
        self.assertEqual(harness.atspi_apps(None), ["Firefox", "org.gnome.Nautilus"])

    def test_the_stage_two_failure_path_collects_the_live_session_state(self):
        source = (RAW / "raw_e2e.py").read_text()
        self.assertIn("lambda: self.live_session_diagnose(runner, label)", source)
        self.assertIn("stall=self.live_session_stall", source)


class OptionalEncryptionTests(unittest.TestCase):
    """gnome-setup's "Encrypt Disk" switch: encrypted by default, the user may turn it off."""

    workflow = yaml.safe_load(WORKFLOW.read_text())
    definitions = RAW / "src/usr/libexec/utah-live-definitions"
    post = RAW / "src/usr/libexec/utah-sysinstall-post"
    bios = "21686148-6449-6e6f-744e-656564454649"
    table = {"partitiontable": {"sectorsize": 512, "partitions": [
        {"node": "/dev/vda1", "type": "21686148-6449-6E6F-744E-656564454649",
         "uuid": "0A0A0A0A-0000-4000-8000-000000000001", "size": 2048, "name": "BIOS-BOOT"},
        {"node": "/dev/vda2", "type": "C12A7328-F81F-11D2-BA4B-00A0C93EC93B",
         "uuid": "0A0A0A0A-0000-4000-8000-000000000002", "size": 1024000, "name": "EFI-SYSTEM"},
        {"node": "/dev/vda3", "type": "0FC63DAF-8483-4772-8E79-3D69D8477DE4",
         "uuid": "0A0A0A0A-0000-4000-8000-000000000003", "size": 2048000, "name": "boot"},
        {"node": "/dev/vda4", "type": "4F68BCE3-E8CD-4DB1-96E7-FBCAF984B709",
         "uuid": "0A0A0A0A-0000-4000-8000-000000000004", "size": 9000000, "name": "root"},
    ]}}

    def generate(self, tpm2):
        """Run utah-live-definitions' generator over a medium's sfdisk table."""
        text = self.definitions.read_text()
        script = text[text.index("<<'PY'\n") + len("<<'PY'\n"):text.index("\nPY\n")]
        with tempfile.TemporaryDirectory() as d:
            out, plain = Path(d) / "repart.d", Path(d) / "repart-unencrypted.d"
            out.mkdir()
            plain.mkdir()
            subprocess.run([sys.executable, "-", str(out), "/dev/vda4", "1" if tpm2 else "0", self.bios,
                            json.dumps(self.table), str(plain)], input=script, text=True, check=True)
            return ({f.name: f.read_text() for f in sorted(out.iterdir())},
                    {f.name: f.read_text() for f in sorted(plain.iterdir())})

    def test_both_definition_sets_copy_the_same_partitions_only_one_encrypts_the_root(self):
        for tpm2, encrypt in ((True, ["Encrypt=key-file+tpm2", "TPM2PCRs=7"]), (False, ["Encrypt=key-file"])):
            with self.subTest(tpm2=tpm2):
                encrypted, plain = self.generate(tpm2)
                self.assertEqual(sorted(encrypted), ["02-copy.conf", "03-copy.conf", "04-copy.conf"])
                self.assertEqual(sorted(plain), sorted(encrypted))
                for name in ("02-copy.conf", "03-copy.conf"):
                    self.assertEqual(plain[name], encrypted[name])
                root = encrypted["04-copy.conf"].splitlines()
                self.assertEqual(root[-len(encrypt):], encrypt)
                self.assertEqual(plain["04-copy.conf"].splitlines(), root[:-len(encrypt)])
                self.assertIn("CopyBlocks=/dev/disk/by-partuuid/0a0a0a0a-0000-4000-8000-000000000004",
                              plain["04-copy.conf"])
                for text in plain.values():
                    self.assertNotIn("Encrypt=", text)
                    self.assertNotIn("TPM2PCRs=", text)

    def test_gnome_setup_is_pointed_at_the_unencrypted_definitions(self):
        pages = configparser.ConfigParser(interpolation=None)
        pages.read_string((RAW / "src/usr/share/utah/gnome-setup/pages.conf").read_text())
        path = pages["page:installer"]["unencrypted-definitions"]
        self.assertEqual(path, "/run/utah-live/repart-unencrypted.d")
        self.assertEqual(path, raw_e2e.PLAIN_DEFINITIONS)
        self.assertIn(f"out_plain={path}\n", self.definitions.read_text())
        steps = pages["modes"]["live-initial-setup"].split(";")
        self.assertLess(steps.index("installer"), steps.index("disk-passphrase"))

    def test_post_install_takes_a_plain_btrfs_root_and_leaves_luks_settings_to_luks(self):
        lines = self.post.read_text().splitlines()
        self.assertIn('    elif [[ "${fstype}" == crypto_LUKS ]]; then root_part="${part}" luks=1', lines)
        self.assertIn('    elif [[ "${fstype}" == btrfs ]]; then root_part="${part}"', lines)
        self.assertIn('    root_dev="${root_part}"', lines)
        guarded = set()
        for start, line in enumerate(lines):
            if line == 'if [[ "${luks}" == 1 ]]; then':
                end = next(i for i in range(start, len(lines)) if lines[i] in ("fi", "else"))
                guarded.update(range(start, end))
        for needle in ("cryptsetup luksUUID", "crypt_activate_by_passphrase(", 'root_dev="/dev/mapper/',
                       "rd.luks.uuid=${luks_uuid}", '>"$d/etc/crypttab"'):
            with self.subTest(needle=needle):
                at = [i for i, line in enumerate(lines) if needle in line]
                self.assertTrue(at)
                self.assertTrue(all(i in guarded for i in at), f"{needle!r} runs for a plain root too")
        self.assertIn('[[ "${luks}" == 0 ]] || cryptsetup close "${mapper}"', lines)

    def test_the_harness_takes_the_encryption_mode(self):
        base = ["--raw", "m.raw", "--work", "w", "--candidate", "r@sha256:0", "--ssh-key", "k"]
        self.assertEqual(raw_e2e.arg_parser().parse_args(base).encryption, "on")
        self.assertEqual(raw_e2e.arg_parser().parse_args(base + ["--encryption", "off"]).encryption, "off")
        with mock.patch("sys.stderr"), self.assertRaises(SystemExit):
            raw_e2e.arg_parser().parse_args(base + ["--encryption", "maybe"])

    def test_unencrypted_boots_expect_no_prompt_at_all(self):
        phases = ("first boot", "after bootc switch", "after bootc rollback")
        plain = raw_e2e.unlock_plan(False, tpm=True)
        self.assertEqual([plain[p][0] for p in phases], ["none"] * 3)
        self.assertEqual([raw_e2e.unlock_plan(True, tpm=True)[p][0] for p in phases], ["tpm", "recovery", "tpm"])
        self.assertEqual([raw_e2e.unlock_plan(True, tpm=False)[p][0] for p in phases], ["pin", "recovery", "pin"])
        self.assertEqual(raw_e2e.unlock_plan(True, tpm=False)["first boot"][1], "12-first-boot-luks-prompt")
        self.assertEqual(len({label for _, label in plain.values()}), 3)

    def test_a_plain_root_has_no_dm_crypt_crypttab_or_luks_karg(self):
        clean = raw_e2e.parse_facts("root_source=/dev/vda4\nroot_types=part disk \ncrypttab=\nluks_kargs=\n")
        self.assertEqual(raw_e2e.plain_root_problems(clean), [])
        for facts, problem in (
            ({**clean, "root_source": "/dev/mapper/luks-1", "root_types": "crypt part disk"}, "dm-crypt"),
            ({**clean, "crypttab": "luks-1 UUID=1 none discard;"}, "/etc/crypttab"),
            ({**clean, "luks_kargs": "rd.luks.uuid=1"}, "rd.luks.uuid=1"),
            ({**clean, "root_types": ""}, "no block device"),
        ):
            with self.subTest(problem=problem):
                problems = raw_e2e.plain_root_problems(facts)
                self.assertEqual(len(problems), 1)
                self.assertIn(problem, problems[0])
        self.assertIn("lsblk -nso TYPE", raw_e2e.PLAIN_ROOT_FACTS)

    @staticmethod
    def switch_nodes(checked, role="switch"):
        states = ["sensitive", "showing"] + (["checked"] if checked else [])
        return [{"path": "0.1", "role": "label", "name": "Encrypt Disk", "text": "Encrypt Disk",
                 "ext": [0, 0, 10, 10], "states": ["sensitive", "showing"]},
                {"path": "0.0", "role": role, "name": "Encrypt Disk", "text": "",
                 "ext": [0, 0, 100, 40], "states": states}]

    def test_the_switch_is_found_by_role_not_by_its_title_label(self):
        for role in raw_e2e.SWITCH_ROLES:
            with self.subTest(role=role):
                self.assertEqual(raw_e2e.encrypt_switch(self.switch_nodes(True, role))["path"], "0.0")
        self.assertIsNone(raw_e2e.encrypt_switch(self.switch_nodes(True)[:1]))
        self.assertTrue(raw_e2e.switch_is(True)(self.switch_nodes(True)))
        self.assertFalse(raw_e2e.switch_is(False)(self.switch_nodes(True)))
        self.assertTrue(raw_e2e.switch_is(False)(self.switch_nodes(False)))

    @staticmethod
    def harness(encrypt):
        harness = object.__new__(raw_e2e.Harness)
        harness.encrypt = encrypt
        harness.results = raw_e2e.Results("gui")
        harness.press = mock.Mock()
        harness.shot = mock.Mock()
        return harness

    def test_the_switch_is_on_by_default_and_turned_off_only_when_asked(self):
        on = self.harness(True)
        self.assertTrue(on.choose_encryption(None, self.switch_nodes(True), 0))
        on.press.assert_not_called()
        self.assertEqual(on.results.encryption["switch_default"], "on")
        off = self.harness(False)
        self.assertFalse(off.choose_encryption(None, self.switch_nodes(True), 0))
        args, kwargs = off.press.call_args
        self.assertEqual(args[1:3], (raw_e2e.GS_APP, "Encrypt Disk"))
        self.assertEqual(kwargs["role"], "switch")
        self.assertTrue(off.choose_encryption(None, self.switch_nodes(False), 5))
        self.assertEqual(off.press.call_count, 1)
        with self.assertRaises(raw_e2e.Fail):
            self.harness(True).choose_encryption(None, self.switch_nodes(False), 0)

    def test_a_disk_page_without_the_switch_fails_after_a_grace(self):
        harness = self.harness(True)
        self.assertFalse(harness.choose_encryption(None, [], 5))
        with self.assertRaises(raw_e2e.Fail):
            harness.choose_encryption(None, [], 30)

    def test_the_confirmation_must_not_contradict_the_switch(self):
        def says(text):
            return raw_e2e.confirm_says([{"name": "", "text": text}])
        self.assertEqual(says("Erase all data? The disk will not be encrypted."), "unencrypted")
        self.assertEqual(says("The disk will be encrypted, and you will need to save a recovery key."), "encrypted")
        self.assertIsNone(says("Begin Installation?"))
        # Neither sentence may make the confirmation read as another page.
        for text in (raw_e2e.CONFIRM_ENCRYPTED_TEXT, raw_e2e.CONFIRM_PLAIN_TEXT):
            nodes = [{"path": "0", "role": "label", "name": "Begin Installation?", "text": "",
                      "states": ["showing"]},
                     {"path": "1", "role": "label", "name": text, "text": "", "states": ["showing"]}]
            self.assertIs(raw_e2e.classify_setup_page(nodes), raw_e2e.Page.CONFIRM)

    def test_ci_runs_an_unencrypted_leg_for_the_main_flavor_on_prs_and_nightly(self):
        on = self.workflow[True]
        dispatch = on["workflow_dispatch"]["inputs"]["encryption"]
        self.assertEqual((dispatch["type"], dispatch["options"], dispatch["default"]), ("choice", ["on", "off"], "on"))
        self.assertEqual(on["workflow_call"]["inputs"]["encryption"]["default"], "on")
        resolve = self.workflow["jobs"]["resolve"]["steps"][-1]
        self.assertEqual(resolve["env"]["ENCRYPTION"], "${{ inputs.encryption || 'on' }}")
        self.assertIn('( "$EVENT" == pull_request || "$EVENT" == schedule )', resolve["run"])
        self.assertIn('[[ "$iso" == true && "$ENCRYPTION" == on', resolve["run"])
        self.assertIn('.encryption = "off" | .iso = false', resolve["run"])
        e2e = self.workflow["jobs"]["e2e"]
        self.assertEqual(e2e["env"]["ENCRYPTION"], "${{ matrix.encryption }}")
        self.assertIn("unencrypted", e2e["name"])
        runs = [s["run"] for s in e2e["steps"] if "python3 iso/raw/raw_e2e.py" in s.get("run", "")]
        self.assertEqual(len(runs), 3)
        for run in runs:
            self.assertIn('--encryption "$ENCRYPTION"', run)
        upload = next(s for s in e2e["steps"] if "upload-artifact" in s.get("uses", ""))
        self.assertIn("-unencrypted", upload["with"]["name"])


if __name__ == "__main__":
    unittest.main()
