"""The desktop contract gates must fail closed on a broken contract.

scripts/verify-desktop-contract.py and scripts/verify-gnome-extensions.py both
gate the build -- the first from `just check` (--check) and from the
Containerfile against a composed image, the second from the GNOME extension
contract -- but neither had any unit coverage, so a gate that silently stopped
rejecting anything would still report success.

This module holds the shared helpers (`ROOT`, `load`, `contract`) and the
contract validation and CLI-mode tests. The rest of the suite lives beside it:
test_desktop_contract_services.py (service presets),
test_desktop_contract_service_edits.py (fwupd drop-in, Dakota's Common edits),
test_desktop_contract_shell.py (GNOME extensions and logos) and
test_desktop_contract_verify.py (in-image verify mode).
"""

import importlib.util
import tomllib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


desktop = load("verify-desktop-contract")
extensions = load("verify-gnome-extensions")

VALID_CONTRACT = {
    "branding": {"files": ["/usr/share/ublue-os/x.png"]},
    "configuration": {"files": []},
    "flatpak": {
        "files": [],
        "apps": ["org.gnome.Calculator", "org.mozilla.firefox"],
        "brewfile": "/usr/share/ublue-os/firstboot/Brewfile",
    },
    "services": {"enabled": ["gdm.service"]},
}


def contract(**overrides):
    merged = {key: dict(value) for key, value in VALID_CONTRACT.items()}
    for key, value in overrides.items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = value
    return merged


class ValidateContractTests(unittest.TestCase):
    def test_valid_contract_has_no_errors(self):
        self.assertEqual(desktop.validate_contract(contract()), [])

    def test_shipped_contract_is_valid(self):
        import tomllib

        data = tomllib.loads((ROOT / "contracts/bluefin-desktop.toml").read_text())
        self.assertEqual(desktop.validate_contract(data), [])

    def test_shipped_contract_keeps_kanata_opt_in_without_input_remapper(self):
        import tomllib

        data = tomllib.loads((ROOT / "contracts/bluefin-desktop.toml").read_text())
        services = data.get("services", {})
        self.assertIn("kanata.service", services.get("disabled", []))
        self.assertNotIn("input-remapper.service", services.get("enabled", []))

    def test_every_missing_section_is_named(self):
        errors = desktop.validate_contract({})
        for section in ("branding", "configuration", "flatpak", "services"):
            self.assertIn(f"missing [{section}] section", errors)

    def test_relative_contract_files_are_rejected(self):
        errors = desktop.validate_contract(
            contract(branding={"files": ["usr/share/relative.png"]})
        )
        self.assertIn("branding file must be absolute: usr/share/relative.png", errors)

    def test_empty_flatpak_app_list_is_rejected(self):
        flatpak = dict(VALID_CONTRACT["flatpak"], apps=[])
        self.assertIn(
            "Flatpak app contract must not be empty",
            desktop.validate_contract(contract(flatpak=flatpak)),
        )

    def test_duplicate_flatpak_ids_are_rejected(self):
        flatpak = dict(VALID_CONTRACT["flatpak"], apps=["org.gnome.Calculator"] * 2)
        self.assertIn(
            "Flatpak app contract contains duplicate IDs",
            desktop.validate_contract(contract(flatpak=flatpak)),
        )

    def test_relative_brewfile_is_rejected(self):
        flatpak = dict(VALID_CONTRACT["flatpak"], brewfile="firstboot/Brewfile")
        self.assertIn(
            "Flatpak Brewfile path must be absolute",
            desktop.validate_contract(contract(flatpak=flatpak)),
        )


class ReadOsReleaseTests(unittest.TestCase):
    def read(self, text):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "os-release"
            path.write_text(text)
            return desktop.read_os_release(path)

    def test_quotes_and_comments_and_blank_lines(self):
        values = self.read('# comment\nID=utah\nNAME="Utah"\n\nPRETTY_NAME="Utah (Version: 1)"\n')
        self.assertEqual(values["ID"], "utah")
        self.assertEqual(values["NAME"], "Utah")
        self.assertEqual(values["PRETTY_NAME"], "Utah (Version: 1)")
        self.assertNotIn("# comment", values)

    def test_value_containing_equals_is_kept_whole(self):
        self.assertEqual(self.read("CPE_NAME=cpe:/o:ub:utah=1\n")["CPE_NAME"], "cpe:/o:ub:utah=1")


class VerifyValuesTests(unittest.TestCase):
    def test_exact_mismatch_names_expected_and_actual(self):
        errors = desktop.verify_values("os-release", {"ID": "fedora"}, {"ID": "utah"}, {})
        self.assertEqual(errors, ["os-release ID must be 'utah', got 'fedora'"])

    def test_missing_key_is_reported_not_skipped(self):
        self.assertEqual(
            desktop.verify_values("os-release", {}, {"ID": "utah"}, {}),
            ["os-release ID must be 'utah', got None"],
        )

    def test_pattern_must_match_the_whole_value(self):
        patterns = {"VARIANT_ID": r"utah(-.*)?"}
        self.assertEqual(desktop.verify_values("x", {"VARIANT_ID": "utah-nvidia"}, {}, patterns), [])
        self.assertEqual(len(desktop.verify_values("x", {"VARIANT_ID": "notutah"}, {}, patterns)), 1)

    def test_missing_key_fails_a_pattern(self):
        self.assertEqual(len(desktop.verify_values("x", {}, {}, {"BUILD_ID": r".+"})), 1)

    def test_non_string_value_is_compared_as_text_for_patterns(self):
        self.assertEqual(desktop.verify_values("x", {"n": 51}, {}, {"n": r"\d+"}), [])


class BaseOsReleaseIdentityTests(unittest.TestCase):
    """Issue #377: keep ID, VERSION_ID and CPE_NAME on the Hummingbird base so
    CVE scanners still recognise the image as Fedora-based. The script stops
    rewriting those fields; the contract must assert the base values instead of
    the utah-branded ones."""

    def shipped_os_release(self):
        import tomllib

        data = tomllib.loads((ROOT / "contracts/bluefin-desktop.toml").read_text())
        return data["branding"]["os_release"], data["branding"]["os_release_patterns"]

    def test_exact_id_assertion_is_the_base_hummingbird(self):
        exact, _ = self.shipped_os_release()
        # The base ships ID=hummingbird; the contract must assert that, not utah.
        self.assertEqual(
            desktop.verify_values("os-release", {"ID": "hummingbird"}, {"ID": exact["ID"]}, {}),
            [],
        )
        self.assertEqual(
            len(desktop.verify_values("os-release", {"ID": "utah"}, {"ID": exact["ID"]}, {})),
            1,
        )

    def test_cpe_pattern_accepts_base_cpe_and_rejects_utah_cpe(self):
        _, patterns = self.shipped_os_release()
        cpe_pattern = {"CPE_NAME": patterns["CPE_NAME"]}
        # A real Hummingbird CPE passes; the utah-branded CPE no longer matches.
        self.assertEqual(
            desktop.verify_values("os-release", {"CPE_NAME": "cpe:/a:redhat:hummingbird:1"}, {}, cpe_pattern),
            [],
        )
        self.assertEqual(
            len(
                desktop.verify_values(
                    "os-release", {"CPE_NAME": "cpe:/o:universal-blue:utah"}, {}, cpe_pattern
                )
            ),
            1,
        )

    def test_contract_no_longer_pins_the_utah_cpe_as_exact(self):
        exact, _ = self.shipped_os_release()
        self.assertNotIn("CPE_NAME", exact)


class ParseBrewfileTests(unittest.TestCase):
    def parse(self, text):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "Brewfile"
            path.write_text(text)
            return desktop.parse_brewfile(path)

    def test_order_is_preserved_and_non_flatpak_lines_ignored(self):
        apps = self.parse(
            '# comment\nbrew "gh"\nflatpak "org.gnome.Calculator"\nflatpak "org.mozilla.firefox"\n'
        )
        self.assertEqual(apps, ["org.gnome.Calculator", "org.mozilla.firefox"])

    def test_indented_entries_are_accepted(self):
        self.assertEqual(self.parse('  flatpak "org.gnome.Loupe"\n'), ["org.gnome.Loupe"])

    def test_trailing_arguments_are_not_treated_as_an_app(self):
        self.assertEqual(self.parse('flatpak "org.gnome.Loupe", args: "x"\n'), [])


class UnitEnabledTests(unittest.TestCase):
    def run_with(self, returncode, stdout):
        result = type("R", (), {"returncode": returncode, "stdout": stdout})()
        with patch.object(desktop.subprocess, "run", return_value=result) as runner:
            enabled = desktop.unit_enabled("gdm.service")
        runner.assert_called_once()
        self.assertEqual(runner.call_args[0][0], ["systemctl", "is-enabled", "gdm.service"])
        return enabled

    def test_enabled_and_enabled_runtime_pass(self):
        self.assertTrue(self.run_with(0, "enabled\n"))
        self.assertTrue(self.run_with(0, "enabled-runtime\n"))

    def test_static_and_masked_and_nonzero_exit_fail(self):
        self.assertFalse(self.run_with(0, "static\n"))
        self.assertFalse(self.run_with(1, "masked\n"))
        self.assertFalse(self.run_with(1, "enabled\n"))


class UnitDisabledTests(unittest.TestCase):
    """`unit_disabled` backs the contract's `disabled` list: a unit that is off
    and cannot be turned on passes, anything enabled fails the build."""

    def run_with(self, returncode, stdout):
        result = type("R", (), {"returncode": returncode, "stdout": stdout})()
        with patch.object(desktop, "unit_masked", return_value=False), patch.object(
            desktop.subprocess, "run", return_value=result
        ) as runner:
            disabled = desktop.unit_disabled("ipp-usb.service")
        self.assertEqual(runner.call_args[0][0], ["systemctl", "is-enabled", "ipp-usb.service"])
        return disabled

    def test_disabled_static_and_not_found_count_as_off(self):
        self.assertTrue(self.run_with(1, "disabled\n"))
        # ipp-usb.service has no [Install] section, so is-enabled reports static.
        self.assertTrue(self.run_with(0, "static\n"))
        self.assertTrue(self.run_with(1, "not-found\n"))

    def test_enabled_states_fail(self):
        for state in ("enabled", "enabled-runtime", "indirect", "alias", "generated"):
            with self.subTest(state=state):
                self.assertFalse(self.run_with(0, f"{state}\n"))

    def test_a_masked_unit_counts_as_off_without_asking_systemctl(self):
        with patch.object(desktop, "unit_masked", return_value=True), patch.object(
            desktop.subprocess, "run"
        ) as runner:
            self.assertTrue(desktop.unit_disabled("dnsmasq.service"))
        runner.assert_not_called()

    def test_missing_systemctl_fails_closed(self):
        with patch.object(desktop, "unit_masked", return_value=False), patch.object(
            desktop.subprocess, "run", side_effect=FileNotFoundError
        ):
            self.assertFalse(desktop.unit_disabled("dnsmasq.service"))


class CheckModeTests(unittest.TestCase):
    def run_check(self, text):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "contract.toml"
            path.write_text(text)
            with patch.object(desktop.sys, "argv", ["verify", "--check", str(path)]):
                return desktop.main()

    def test_check_accepts_the_shipped_contract(self):
        with patch.object(
            desktop.sys, "argv", ["verify", "--check", str(ROOT / "contracts/bluefin-desktop.toml")]
        ):
            self.assertEqual(desktop.main(), 0)

    def test_check_rejects_a_contract_missing_sections(self):
        self.assertEqual(self.run_check('[branding]\nfiles = []\n'), 1)

    def test_check_does_not_touch_the_image_filesystem(self):
        with patch.object(desktop, "read_os_release") as reader, patch.object(
            desktop, "unit_enabled"
        ) as unit:
            with patch.object(
                desktop.sys,
                "argv",
                ["verify", "--check", str(ROOT / "contracts/bluefin-desktop.toml")],
            ):
                self.assertEqual(desktop.main(), 0)
        reader.assert_not_called()
        unit.assert_not_called()


class FlatpaksModeTests(unittest.TestCase):
    """`--flatpaks BREWFILE` is the single entry point for the Brewfile flatpak
    set (see #510/#511). Print the ordered app IDs and exit 0; reject a
    contract argument in --flatpaks mode and require a contract when neither
    mode is selected.
    """

    def run_flatpaks(self, brewfile_text):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "Brewfile"
            path.write_text(brewfile_text)
            with patch.object(
                desktop.sys, "argv", ["verify", "--flatpaks", str(path)]
            ), patch("sys.stdout", new_callable=__import__("io").StringIO) as out:
                rc = desktop.main()
                return rc, out.getvalue()

    def test_prints_ordered_app_ids(self):
        rc, out = self.run_flatpaks(
            '# comment\nbrew "gh"\nflatpak "org.gnome.Calculator"\n'
            'flatpak "org.mozilla.firefox"\n'
        )
        self.assertEqual(rc, 0)
        self.assertEqual(
            out.splitlines(), ["org.gnome.Calculator", "org.mozilla.firefox"]
        )

    def test_rejects_a_contract_argument(self):
        with tempfile.TemporaryDirectory() as tmp:
            brewfile = Path(tmp) / "Brewfile"
            brewfile.write_text('flatpak "org.gnome.Calculator"\n')
            with patch.object(
                desktop.sys,
                "argv",
                ["verify", "--flatpaks", str(brewfile), str(ROOT / "contracts/bluefin-desktop.toml")],
            ):
                with self.assertRaises(SystemExit) as cm:
                    desktop.main()
                self.assertEqual(cm.exception.code, 2)

    def test_requires_a_contract_when_neither_mode_is_given(self):
        with patch.object(desktop.sys, "argv", ["verify"]):
            with self.assertRaises(SystemExit) as cm:
                desktop.main()
            self.assertEqual(cm.exception.code, 2)


class ServicesModeTests(unittest.TestCase):
    """`--services LIST CONTRACT` prints one [services] list, one unit per line.

    iso/scripts/e2e-services.sh reads the installed contract this way to check
    every listed unit on the booted system (#19). It must not run the image
    checks.
    """

    def run_services(self, kind):
        contract = ROOT / "contracts/bluefin-desktop.toml"
        with patch.object(desktop.sys, "argv", ["verify", "--services", kind, str(contract)]), \
                patch.object(desktop, "unit_enabled") as unit, \
                patch("sys.stdout", new_callable=__import__("io").StringIO) as out:
            rc = desktop.main()
        unit.assert_not_called()
        return rc, out.getvalue().splitlines()

    def test_prints_each_list_from_the_contract(self):
        services = tomllib.loads((ROOT / "contracts/bluefin-desktop.toml").read_text())["services"]
        for kind in ("enabled", "user_enabled"):
            rc, units = self.run_services(kind)
            self.assertEqual(rc, 0)
            self.assertEqual(units, services[kind])
            self.assertTrue(units, kind)

    def test_rejects_other_lists(self):
        with patch.object(desktop.sys, "argv", ["verify", "--services", "masked", "x.toml"]), \
                patch.object(desktop.sys, "stderr"):
            with self.assertRaises(SystemExit) as cm:
                desktop.main()
        self.assertEqual(cm.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
