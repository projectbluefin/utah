"""The desktop contract gates must fail closed on a broken contract.

scripts/verify-desktop-contract.py and scripts/verify-gnome-extensions.py both
gate the build -- the first from `just check` (--check) and from the
Containerfile against a composed image, the second from the GNOME extension
contract -- but neither had any unit coverage, so a gate that silently stopped
rejecting anything would still report success.
"""

import importlib.util
import json
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

    def test_shipped_contract_enables_input_remapper(self):
        import tomllib

        data = tomllib.loads((ROOT / "contracts/bluefin-desktop.toml").read_text())
        self.assertIn("input-remapper.service", data.get("services", {}).get("enabled", []))

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


class GnomeExtensionTests(unittest.TestCase):
    def shipped_tree(self, tmp, versions="51", uuids=None):
        base = Path(tmp) / "usr/share/gnome-shell/extensions"
        for uuid in uuids if uuids is not None else extensions.SOURCE_EXTENSIONS:
            path = base / uuid / "metadata.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"shell-version": [versions]}))
        return Path(tmp)

    def run_main(self, root):
        with patch.object(extensions.sys, "argv", ["verify", "--root", str(root)]):
            return extensions.main()

    def test_complete_tree_declaring_gnome_51_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self.run_main(self.shipped_tree(tmp)), 0)

    def test_missing_extension_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            uuids = list(extensions.SOURCE_EXTENSIONS)[1:]
            self.assertEqual(self.run_main(self.shipped_tree(tmp, uuids=uuids)), 1)

    def test_extension_without_gnome_51_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self.run_main(self.shipped_tree(tmp, versions="50")), 1)

    def test_invalid_metadata_fails_instead_of_raising(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.shipped_tree(tmp)
            uuid = next(iter(extensions.SOURCE_EXTENSIONS))
            (root / f"usr/share/gnome-shell/extensions/{uuid}/metadata.json").write_text("{")
            self.assertEqual(self.run_main(root), 1)

    def test_shell_versions_rejects_metadata_without_the_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "metadata.json"
            path.write_text(json.dumps({"name": "x"}))
            with self.assertRaises(ValueError):
                extensions.shell_versions(path)

    def test_shell_versions_substitutes_the_gsconnect_version_placeholder(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "metadata.json.in"
            path.write_text('{"version": @PACKAGE_VERSION@, "shell-version": ["51"]}')
            self.assertEqual(extensions.shell_versions(path), ["51"])

    def test_source_mode_matches_the_checked_in_submodule_paths(self):
        for uuid, relative in extensions.SOURCE_EXTENSIONS.items():
            self.assertTrue(
                relative == uuid + "/metadata.json" or relative.endswith("metadata.json")
                or relative.endswith("metadata.json.in"),
                f"{uuid} source path does not point at extension metadata: {relative}",
            )


class ServiceMaskParityTests(unittest.TestCase):
    def test_bootc_fetch_apply_updates_masked_and_disabled(self):
        preset = (ROOT / "system_files/shared/usr/lib/systemd/system-preset/85-utah-desktop.preset").read_text()
        self.assertIn("disable bootc-fetch-apply-updates.timer", preset)
        self.assertIn("disable bootc-fetch-apply-updates.service", preset)

        config_services = (ROOT / "scripts/configure-services.sh").read_text()
        self.assertIn("systemctl mask bootc-fetch-apply-updates.timer bootc-fetch-apply-updates.service", config_services)
        self.assertIn("ln -sf /dev/null /usr/lib/systemd/system/bootc-fetch-apply-updates.timer", config_services)
        self.assertIn("ln -sf /dev/null /usr/lib/systemd/system/bootc-fetch-apply-updates.service", config_services)
        self.assertIn("bootc-fetch-apply-updates", config_services)

    def test_cross_vendor_merge_and_switch_mask_documented(self):
        readme = (ROOT / "README.md").read_text()
        desktop_skill = (ROOT / "docs/skills/desktop-contract.md").read_text()
        testing_skill = (ROOT / "docs/skills/local-testing.md").read_text()

        self.assertIn("bootc-fetch-apply-updates", readme)
        self.assertIn("3-way", readme)
        self.assertIn("rollback", readme)
        self.assertIn("systemctl is-enabled bootc-fetch-apply-updates.timer", readme)

        self.assertIn("bootc-fetch-apply-updates.timer", desktop_skill)
        self.assertIn("bootc-fetch-apply-updates.service", desktop_skill)
        self.assertIn("uupd.timer", desktop_skill)

        self.assertIn("bootc-fetch-apply-updates.timer", testing_skill)
        self.assertIn("uupd.timer", testing_skill)
        self.assertIn("rollback", testing_skill)

    def test_desktop_contract_declares_masked_services(self):
        import tomllib
        contract = tomllib.loads((ROOT / "contracts/bluefin-desktop.toml").read_text())
        masked = contract.get("services", {}).get("masked", [])
        self.assertIn("bootc-fetch-apply-updates.timer", masked)
        self.assertIn("bootc-fetch-apply-updates.service", masked)

    def test_unit_masked_helper_verifies_dev_null_symlink(self):
        import tempfile
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "verify_desktop_contract", ROOT / "scripts/verify-desktop-contract.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lib_dir = root / "usr/lib/systemd/system"
            lib_dir.mkdir(parents=True)
            unit_symlink = lib_dir / "bootc-fetch-apply-updates.timer"
            unit_symlink.symlink_to("/dev/null")

            self.assertTrue(module.unit_masked("bootc-fetch-apply-updates.timer", root=root))
            self.assertFalse(module.unit_masked("bootc-fetch-apply-updates.service", root=root))

    def test_unit_masked_helper_systemctl_fallback(self):
        import importlib.util
        from unittest.mock import patch
        import subprocess
        spec = importlib.util.spec_from_file_location(
            "verify_desktop_contract", ROOT / "scripts/verify-desktop-contract.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        # systemctl is-enabled returns exit code 1 with stdout "masked\n" when a unit is masked
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["systemctl", "is-enabled", "masked-sample.service"],
                returncode=1,
                stdout="masked\n",
                stderr="",
            )
            # When root is "/", fallback to systemctl queries host and returns True
            self.assertTrue(module.unit_masked("masked-sample.service", root=Path("/")))
            mock_run.assert_called_once_with(
                ["systemctl", "is-enabled", "masked-sample.service"],
                capture_output=True,
                text=True,
                check=False,
            )

        # When unit is not masked (e.g. "disabled" with exit code 1)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["systemctl", "is-enabled", "disabled-sample.service"],
                returncode=1,
                stdout="disabled\n",
                stderr="",
            )
            self.assertFalse(module.unit_masked("disabled-sample.service", root=Path("/")))

        # When root is not "/", fallback to systemctl is skipped to prevent host pollution
        with patch("subprocess.run") as mock_run:
            self.assertFalse(module.unit_masked("masked-sample.service", root=Path("/tmp/other-root")))
            mock_run.assert_not_called()



if __name__ == "__main__":
    unittest.main()
