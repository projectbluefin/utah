"""Replay installer page routes without a guest, SSH, or QEMU."""
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location(
    "setup_raw_e2e", Path(__file__).resolve().parents[1] / "iso/raw/raw_e2e.py")
raw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(raw)

KEY = "abcdefgh-abcdefgh-abcdefgh-abcdefgh-abcdefgh-abcdefgh-abcdefgh-abcdefgh"


def node(name, role="label", states=None):
    return {"name": name, "text": "", "role": role, "path": name,
            "ext": [0, 0, 100, 30],
            "states": ["showing", "sensitive"] if states is None else states}


def page(heading, *buttons):
    return [node(heading)] + [node(b, "button") for b in buttons]


def disk_page(target_status=raw.DISK_EMPTY_TEXT):
    """gnome-setup's disk list: the blank target and the live medium, each a
    row whose title and subtitle labels sit below it."""
    def row(path, title, subtitle):
        return [dict(node(title, "table row"), path=path),
                dict(node(title), path=path + ".0"), dict(node(subtitle), path=path + ".1")]
    return ([node("Select a Disk")]
            + row("1.0", "Unknown Disk 51.5\u00a0GB", target_status)
            + row("1.1", "Unknown Disk 9.7\u00a0GB", raw.DISK_DATA_TEXT))


def empty_disk_confirm():
    """confirm::nondestructive over the disk page: names no disk."""
    return disk_page() + [
        node("Begin Installation?"),
        node("Once the installation begins, the changes to the selected disk are irreversible. "
             "Continue with installation?"),
        node("Cancel", "button"), node("Install", "button")]


class SetupPages(unittest.TestCase):
    def harness(self, snapshots):
        h = object.__new__(raw.Harness)
        h.tpm = True
        h.recovery_key = None
        h.timings = {}
        h.snapshot = Mock(side_effect=snapshots)
        h.atspi = Mock(return_value={"how": "action:click"})
        h.save = Mock()
        h.shot = Mock()
        h.click_node = Mock()
        h.fill = Mock()
        return h

    def run_loop(self, h, clock=None):
        if clock is None:
            clock = iter(range(10000))
        with patch.object(raw.time, "sleep"), patch.object(raw.time, "monotonic", side_effect=clock):
            h.drive_setup(None)

    def recovery(self):
        return [node("Back Up Recovery Key"), node(KEY),
                node("Show QR Code for Recovery Key", "button")]

    def test_empty_disk_route_completes(self):
        route = [page("Language", "English", "Next"),
                 page("Keyboard Layout", "Next"), page("Welcome", "Install Now"),
                 disk_page(), empty_disk_confirm(),
                 page("Installing"), self.recovery(),
                 page("Scan to Back Up", "Done"),
                 [node("Back Up Recovery Key"), node(KEY),
                  node("I backed up my recovery key", "check box")],
                 [node("Back Up Recovery Key"), node(KEY), node("Done", "button")],
                 page("Installation Complete")]
        h = self.harness(route)
        self.run_loop(h)
        self.assertEqual(h.recovery_key, KEY)
        names = [c.args[3] for c in h.atspi.call_args_list]
        self.assertIn("Install", names)
        self.assertEqual(names.count("Done"), 2)
        self.assertEqual(h.click_node.call_args_list[-1].args[1]["path"], "1.0")
        labels = [c.args[0] for c in h.shot.call_args_list]
        self.assertIn("setup-confirm", labels)
        self.assertEqual(len(labels), len(set(labels)))

    def test_existing_data_on_the_blank_target_fails_before_any_press(self):
        found = [node("Existing Data Found"), node("Install to Free Space", "row"),
                 node("Start Installation", "button")]
        erase = [node("Begin Installation?"), node("Cancel", "button"), node("Erase and Install", "button")]
        forced = [node("Insufficient Free Space"), node("Erase and Install (3)", "button")]
        for name, screen in (("data page", found), ("erase confirm", erase),
                             ("forced erase", forced), ("disk row", disk_page(raw.DISK_DATA_TEXT))):
            with self.subTest(name):
                h = self.harness([disk_page(), screen] if name != "disk row" else [screen])
                with self.assertRaisesRegex(raw.Fail, "existing data on the blank target disk"):
                    self.run_loop(h)
                h.atspi.assert_not_called()
                self.assertLessEqual(h.click_node.call_count, 1)

    def test_failure_wins_and_hidden_text_is_ignored(self):
        self.assertIs(raw.classify_setup_page(page("Installation Complete Installation Failed")), raw.Page.FAILURE)
        self.assertIs(raw.classify_setup_page([
            node("Installation Failed", states=[]), node("Back Up Recovery Key"), node(KEY)]), raw.Page.RECOVERY_KEY)
        self.assertIs(raw.classify_setup_page(page("Something new")), raw.Page.UNKNOWN)
        self.assertIs(raw.classify_setup_page(page("Scan to Back Up Back Up Recovery Key")), raw.Page.QR)

    def test_confirm_without_selecting_the_target_is_refused(self):
        h = self.harness([empty_disk_confirm()])
        with self.assertRaisesRegex(raw.Fail, "target row was never selected"):
            self.run_loop(h)
        h.atspi.assert_not_called()

    def test_skipped_pages_and_transient_unknown_are_allowed(self):
        h = self.harness([page("Drawing..."), self.recovery(),
                          page("Scan to Back Up", "Done"),
                          [node("Back Up Recovery Key"), node(KEY), node("Done", "button")],
                          page("Installation Complete")])
        self.run_loop(h)

    def test_unknown_and_progress_have_bounded_waits(self):
        for heading, ticks in (("New page", [0, 0, 0, 61, 61]),
                               ("Installing", [0, 0, 0, 3601, 3601])):
            h = self.harness([page(heading), page(heading)])
            with self.assertRaisesRegex(raw.Fail, "stuck on"):
                self.run_loop(h, ticks)

    def test_no_tpm_fills_passphrase_before_advancing(self):
        entries = [node("Choose a Disk Passphrase"), node("Passphrase", "password text"),
                   node("Confirm Passphrase", "password text"), node("Next", "button")]
        h = self.harness([entries, entries, page("Installation Failed")])
        h.tpm = False
        with self.assertRaisesRegex(raw.Fail, "installation failed"):
            self.run_loop(h)
        self.assertEqual(h.fill.call_count, 2)
        self.assertEqual(h.atspi.call_args.args[3], "Next")

    def test_missing_button_dumps_hidden_and_insensitive_states(self):
        h = self.harness([])
        nodes = [node("Install", "push button", ["showing"]),
                 node("Old button", "push button", [])]
        h.atspi.return_value = nodes
        self.assertFalse(h.setup_press(None, nodes, "Install"))
        saved = json.loads(h.save.call_args.args[1])
        self.assertEqual(saved, nodes)
        self.assertIn("sensitive=False", raw.summarize_nodes(saved)[0])
        h.atspi.assert_called_once_with(None, "snapshot-all", raw.GS_APP)

    def test_startup_waits_for_recognized_page_before_window_actions(self):
        h = self.harness([])
        h.live_runner = None
        h.results = Mock()
        h.wait_for = Mock(return_value=page("Welcome", "Install Now"))
        h.key = Mock()
        h.maximize = Mock()
        h.drive_setup = Mock()
        h.grab_install_log = Mock()
        h.grab_sysinstall_journal = Mock()
        h.check_slots = Mock()
        h.after_install = Mock()
        order = Mock()
        for name in ("wait_for", "key", "maximize", "drive_setup"):
            order.attach_mock(getattr(h, name), name)
        with patch.object(raw.time, "sleep") as sleep:
            h.gnome_setup_install()
        args = h.wait_for.call_args.args
        self.assertEqual(args[3:], ("first setup page", 300))
        self.assertFalse(args[2]([]))
        self.assertFalse(args[2](page("Drawing...")))
        self.assertTrue(args[2](page("Welcome", "Install Now")))
        self.assertTrue(args[2](page("Installation Failed")))
        self.assertEqual([c[0] for c in order.mock_calls],
                         ["wait_for", "key", "maximize", "drive_setup"])
        sleep.assert_called_once_with(3)

    def test_failure_collects_tree_screenshot_and_logs(self):
        h = self.harness([page("Installation Failed")] * 2)
        h.live_runner = None
        h.key = Mock()
        h.maximize = Mock()
        h.results = Mock()
        h.grab_install_log = Mock()
        h.grab_sysinstall_journal = Mock()
        with patch.object(raw.time, "sleep"):
            with self.assertRaisesRegex(raw.Fail, "installation failed"):
                h.gnome_setup_install()
        self.assertTrue(any(c.args[0] == "atspi-gs-failure.json" for c in h.save.call_args_list))
        self.assertTrue(any(c.args[0] == "fail-setup-failure" for c in h.shot.call_args_list))
        h.grab_install_log.assert_called_once()
        h.grab_sysinstall_journal.assert_called_once()


class InitialSetupTimeZone(unittest.TestCase):
    """gnome-initial-setup's Time Zone page without a GeoClue location (run 45926390728)."""

    def time_zone(self, located):
        insensitive = ["showing", "visible", "focusable"]
        return [node("Time Zone", "heading"),
                node("The time zone will be set automatically if your location can be found. "
                     "You can also search for a city to set it yourself."),
                node("", "text", ["editable", "focused", "showing", "sensitive"]),
                node("Next", "button", None if located else insensitive),
                node("Next")]

    def harness(self, polls):
        h = object.__new__(raw.Harness)
        h.fill = Mock()
        h.key = Mock()
        h.poll = Mock(side_effect=polls)
        h.snapshot = Mock(return_value=self.time_zone(False))
        h.save = Mock()
        h.shot = Mock()
        return h

    def test_the_label_of_an_insensitive_next_is_not_a_next_button(self):
        self.assertIsNone(raw.next_button(self.time_zone(False)))
        self.assertEqual(raw.next_button(self.time_zone(True))["role"], "button")

    def test_search_picks_a_city_until_next_is_sensitive(self):
        located = self.time_zone(True)
        h = self.harness([None, located])
        entry = self.time_zone(False)[2]
        with patch.object(raw.time, "sleep"):
            self.assertIs(h.search_time_zone(None, raw.GIS_APP, entry), located)
        self.assertEqual(h.fill.call_count, 2)
        h.fill.assert_called_with(None, raw.GIS_APP, entry, raw.TIMEZONE_CITY)
        self.assertEqual([c.args[0] for c in h.key.call_args_list], ["down", "ret"] * 2)
        done = h.poll.call_args.args[2]
        self.assertFalse(done(self.time_zone(False)))
        self.assertTrue(done(located))
        self.assertTrue(done(page("About You", "Next")))

    def test_search_that_never_completes_fails_with_evidence(self):
        h = self.harness([None, None])
        with patch.object(raw.time, "sleep"):
            with self.assertRaisesRegex(raw.Fail, "never completed the Time Zone page"):
                h.search_time_zone(None, raw.GIS_APP, self.time_zone(False)[2])
        h.save.assert_called_once()
        self.assertEqual(h.shot.call_args.args[0], "fail-time-zone-search")


if __name__ == "__main__":
    unittest.main()
