"""Dakota desktop parity: first-boot helpers and login-shell guards (#675).

Utah ingests `projectbluefin/common` as a pinned OCI image, so Dakota's
BuildStream-time customizations have to be re-applied here: the units and
helpers ship under `system_files/shared/`, the preset and
`scripts/configure-services.sh` enable them, and the same script patches
common's files with guards that fail the build on an unrecognized upstream
rewrite. The desktop contract asserts the result. These tests hold the five
pieces (unit files, helpers, preset, script, contract) in agreement so one
cannot be dropped without the others failing.
"""

import importlib.util
import os
import stat
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SYSTEM = ROOT / "system_files/shared"
SCRIPT = ROOT / "scripts/configure-services.sh"
PRESET = SYSTEM / "usr/lib/systemd/system-preset/85-utah-desktop.preset"
CONTRACT_PATH = ROOT / "contracts/bluefin-desktop.toml"

UNITS = (
    "bluefin-wallpaper-month.service",
    "ublue-firstboot-date.service",
    "ublue-booted-image.service",
)

UNIT_DIR = SYSTEM / "usr/lib/systemd/system"


def load_desktop_verifier():
    spec = importlib.util.spec_from_file_location(
        "verify_desktop_contract", ROOT / "scripts/verify-desktop-contract.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ParityUnitFileTests(unittest.TestCase):
    def test_every_parity_unit_ships(self):
        for unit in UNITS:
            with self.subTest(unit=unit):
                self.assertTrue((UNIT_DIR / unit).is_file(), f"missing {unit}")

    def test_every_parity_unit_is_wanted_by_multi_user_target(self):
        for unit in UNITS:
            with self.subTest(unit=unit):
                text = (UNIT_DIR / unit).read_text()
                self.assertIn("[Install]", text)
                self.assertIn("WantedBy=multi-user.target", text)

    def test_wallpaper_service_runs_before_the_greeter(self):
        text = (UNIT_DIR / "bluefin-wallpaper-month.service").read_text()
        self.assertIn("ExecStart=/usr/libexec/bluefin-wallpaper-month-sync", text)
        self.assertIn("Before=display-manager.service", text)

    def test_firstboot_date_is_once_only(self):
        text = (UNIT_DIR / "ublue-firstboot-date.service").read_text()
        self.assertIn("ConditionPathExists=!/var/lib/ublue-os/install-date", text)
        self.assertIn("/var/lib/ublue-os/install-date", text)

    def test_booted_image_snapshot_runs_before_user_sessions(self):
        text = (UNIT_DIR / "ublue-booted-image.service").read_text()
        self.assertIn("ExecStart=/usr/bin/ublue-booted-image-sync", text)
        self.assertIn("Before=systemd-user-sessions.service", text)


class ParityHelperScriptTests(unittest.TestCase):
    def test_wallpaper_sync_maintains_the_month_symlink(self):
        path = SYSTEM / "usr/libexec/bluefin-wallpaper-month-sync"
        text = path.read_text()
        self.assertIn("/var/lib/bluefin/backgrounds", text)
        self.assertIn("current-bluefin.xml", text)
        self.assertIn("date +%m", text)

    def test_booted_image_sync_snapshots_the_booted_ref(self):
        path = SYSTEM / "usr/bin/ublue-booted-image-sync"
        text = path.read_text()
        self.assertIn(".status.booted.image.image.image", text)
        self.assertIn("/run/ublue-os/booted-image", text)

    def test_helper_scripts_are_executable(self):
        for relative in (
            "usr/libexec/bluefin-wallpaper-month-sync",
            "usr/bin/ublue-booted-image-sync",
        ):
            with self.subTest(script=relative):
                mode = (SYSTEM / relative).stat().st_mode
                self.assertTrue(mode & stat.S_IXUSR, f"{relative} is not executable")


class ParityEnablementTests(unittest.TestCase):
    def test_preset_enables_every_parity_unit(self):
        preset = PRESET.read_text()
        for unit in UNITS:
            with self.subTest(unit=unit):
                self.assertIn(f"enable {unit}", preset)

    def test_build_time_script_enables_every_parity_unit(self):
        services = SCRIPT.read_text()
        for unit in UNITS:
            with self.subTest(unit=unit):
                self.assertIn(f"enable_unit {unit}", services)

    def test_script_guards_the_wallpaper_patch(self):
        services = SCRIPT.read_text()
        self.assertIn("/var/lib/bluefin/backgrounds/current-bluefin.xml", services)
        self.assertIn("bluefin-wallpaper-month.service", services)

    def test_script_guards_the_install_date_patch(self):
        services = SCRIPT.read_text()
        self.assertIn("/var/lib/ublue-os/install-date", services)
        self.assertIn("ublue-firstboot-date.service", services)

    def test_script_guards_the_booted_image_patch(self):
        services = SCRIPT.read_text()
        self.assertIn("/run/ublue-os/booted-image", services)
        self.assertIn("ublue-booted-image.service", services)

    def test_script_guards_the_uwelcome_interactive_check(self):
        services = SCRIPT.read_text()
        self.assertIn("case $- in", services)
        self.assertIn("UWELCOME_SHOWN", services)


class ParityContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import tomllib

        cls.contract = tomllib.loads(CONTRACT_PATH.read_text())

    def test_shipped_contract_is_valid(self):
        desktop = load_desktop_verifier()
        self.assertEqual(desktop.validate_contract(self.contract), [])

    def test_parity_units_are_required_enabled(self):
        enabled = self.contract.get("services", {}).get("enabled", [])
        for unit in UNITS:
            with self.subTest(unit=unit):
                self.assertIn(unit, enabled)

    def test_parity_payloads_are_required_files(self):
        files = []
        for section in ("branding", "configuration", "flatpak"):
            files.extend(self.contract.get(section, {}).get("files", []))
        for required in (
            "/usr/lib/systemd/system/bluefin-wallpaper-month.service",
            "/usr/libexec/bluefin-wallpaper-month-sync",
            "/usr/lib/systemd/system/ublue-firstboot-date.service",
            "/usr/lib/systemd/system/ublue-booted-image.service",
            "/usr/bin/ublue-booted-image-sync",
        ):
            with self.subTest(required=required):
                self.assertIn(required, files)

    def test_wallpaper_override_points_at_the_month_symlink(self):
        contains = self.contract.get("configuration", {}).get("file_contains", {})
        override = " ".join(
            contains.get(
                "/usr/share/glib-2.0/schemas/zz0-bluefin-modifications.gschema.override",
                [],
            )
        )
        self.assertIn("/var/lib/bluefin/backgrounds/current-bluefin.xml", override)

    def test_fastfetch_reads_the_stamped_install_date(self):
        contains = self.contract.get("configuration", {}).get("file_contains", {})
        self.assertIn(
            "/var/lib/ublue-os/install-date",
            " ".join(contains.get("/usr/share/ublue-os/fastfetch.jsonc", [])),
        )

    def test_image_info_prefers_the_booted_snapshot(self):
        contains = self.contract.get("configuration", {}).get("file_contains", {})
        self.assertIn(
            "/run/ublue-os/booted-image",
            " ".join(contains.get("/usr/bin/ublue-image-info.sh", [])),
        )

    def test_uwelcome_guards_non_interactive_shells(self):
        contains = self.contract.get("configuration", {}).get("file_contains", {})
        self.assertIn(
            "case $- in",
            " ".join(contains.get("/etc/profile.d/uwelcome.sh", [])),
        )


if __name__ == "__main__":
    unittest.main()
