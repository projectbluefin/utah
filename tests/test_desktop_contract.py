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

    def test_serial_getty_ttyS0_masked_and_disabled(self):
        preset = (ROOT / "system_files/shared/usr/lib/systemd/system-preset/85-utah-desktop.preset").read_text()
        self.assertIn("disable serial-getty@ttyS0.service", preset)

        config_services = (ROOT / "scripts/configure-services.sh").read_text()
        self.assertIn("systemctl mask serial-getty@ttyS0.service", config_services)
        self.assertIn(
            "ln -sf /dev/null /usr/lib/systemd/system/serial-getty@ttyS0.service", config_services
        )

        import tomllib

        contract = tomllib.loads((ROOT / "contracts/bluefin-desktop.toml").read_text())
        masked = contract.get("services", {}).get("masked", [])
        self.assertIn("serial-getty@ttyS0.service", masked)

    def test_serial_getty_mask_explains_the_hummingbird_karg(self):
        # The mask only makes sense next to the reason: the karg lives in the
        # base image's kargs.d and cannot be withdrawn from here.
        config_services = (ROOT / "scripts/configure-services.sh").read_text()
        self.assertIn("/usr/lib/bootc/kargs.d/00-base.toml", config_services)
        self.assertIn("#103", config_services)

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

    def test_grub_boot_success_timer_not_enabled(self):
        # /boot is read-only at runtime, so the boot-success mark can never be
        # written and the timer fails on every boot; nothing consumes the flag.
        preset = (ROOT / "system_files/shared/usr/lib/systemd/user-preset/85-utah-desktop.preset").read_text()
        self.assertNotIn("enable grub-boot-success.timer", preset)
        self.assertIn("#364", preset)

        config_services = (ROOT / "scripts/configure-services.sh").read_text()
        config_code = "\n".join(
            line for line in config_services.splitlines()
            if not line.strip().startswith("#")
        )
        self.assertNotIn("grub-boot-success.timer", config_code)

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


class GdmGreeterLogoTests(unittest.TestCase):
    """Issue #378: the GDM greeter logo must point at the Bluefin mark.

    Without an ``org.gnome.login-screen.logo`` override in the ``gdm`` dconf
    profile, gnome-shell falls back to ``/usr/share/pixmaps/fedora-gdm-logo.png``
    from ``fedora-logos``, and the login screen shows the Fedora wordmark
    instead of Bluefin. The fix is a keyfile under
    ``/etc/dconf/db/gdm.d/`` plus a desktop-contract assertion that catches a
    regression at build time rather than at the post-install E2E.
    """

    CONTRACT_PATH = ROOT / "contracts/bluefin-desktop.toml"
    KEYFILE_PATH = ROOT / "system_files/shared/etc/dconf/db/gdm.d/01-bluefin-gdm-logo"
    BLUEFIN_LOGO = "/usr/share/pixmaps/bluefin-gdm-logo.png"

    def test_shipped_contract_declares_gdm_keyfile(self):
        import tomllib
        contract = tomllib.loads(self.CONTRACT_PATH.read_text())
        config_files = contract.get("configuration", {}).get("files", [])
        self.assertIn("/etc/dconf/db/gdm.d/01-bluefin-gdm-logo", config_files)

    def test_shipped_contract_asserts_bluefin_logo_in_gdm_keyfile(self):
        import tomllib
        contract = tomllib.loads(self.CONTRACT_PATH.read_text())
        file_contains = (
            contract.get("configuration", {}).get("file_contains", {})
        )
        gdm_expectations = file_contains.get(
            "/etc/dconf/db/gdm.d/01-bluefin-gdm-logo"
        )
        # The contract must require both the schema header and the logo path:
        # losing either is the exact regression that lets the Fedora fallback
        # resurface (the schema without the path, or the path without the
        # schema).
        self.assertIsNotNone(gdm_expectations)
        self.assertIn("[org/gnome/login-screen]", gdm_expectations)
        self.assertIn(f"logo='{self.BLUEFIN_LOGO}'", gdm_expectations)

    def test_keyfile_points_at_shipped_bluefin_logo(self):
        """The on-disk keyfile must reference the asset the contract asserts."""
        content = self.KEYFILE_PATH.read_text()
        self.assertIn("[org/gnome/login-screen]", content)
        self.assertIn(f"logo='{self.BLUEFIN_LOGO}'", content)

    def test_logo_is_a_small_wordmark_shipped_in_the_overlay(self):
        """gnome-shell draws the greeter logo at natural size.

        The full-size ``bluefin.png`` (372x493) fills the login screen, so the
        logo must be the small wordmark Utah ships itself.
        """
        import struct
        png = ROOT / "system_files/shared" / self.BLUEFIN_LOGO.lstrip("/")
        data = png.read_bytes()
        self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")
        width, height = struct.unpack(">II", data[16:24])
        self.assertLessEqual(width, 256)
        self.assertLessEqual(height, 128)

    def test_keyfile_does_not_reference_fedora_fallback(self):
        """A regression to the Fedora wordmark must be caught at the source.

        Only the active dconf lines matter: ``dconf update`` ignores
        everything after a leading ``#``. Allowing the path in a comment keeps
        the file's preamble informative without weakening the assertion that
        the live override points at Bluefin.
        """
        active_lines = [
            line for line in self.KEYFILE_PATH.read_text().splitlines()
            if line and not line.lstrip().startswith("#")
        ]
        active = "\n".join(active_lines)
        self.assertNotIn("fedora-gdm-logo", active)
        self.assertNotIn("/usr/share/pixmaps/fedora", active)
        self.assertEqual(len(active_lines), 2, f"unexpected active lines: {active_lines}")


class FwupdRefreshDropInTests(unittest.TestCase):
    """fwupd-refresh.service ships with DynamicUser=yes and CacheDirectory=fwupdmgr
    on Fedora/Hummingbird. The combination triggers a pre-existing-public ->
    /var/cache/private migration at every boot; in this bootc image the
    rename returns EACCES (a policy denial on /var/cache, not a plain-ownership
    problem) and the unit exits 1, so firmware metadata never refreshes (#385).

    The drop-in at
    system_files/shared/usr/lib/systemd/system/fwupd-refresh.service.d/
    binds the unit to a static user (fwupd-refresh) AND sets DynamicUser=no
    so the migration code in systemd is skipped, AND re-asserts the
    hardening directives that DynamicUser=yes would have implied so the
    drop-in preserves the same posture. The user is allocated by the
    sysusers.d fragment at
    system_files/shared/usr/lib/sysusers.d/utah-fwupd-refresh.conf.
    """

    DROP_IN = ROOT / "system_files/shared/usr/lib/systemd/system/fwupd-refresh.service.d/10-utah-fwupd-refresh-user.conf"
    SYSUSERS = ROOT / "system_files/shared/usr/lib/sysusers.d/utah-fwupd-refresh.conf"

    def test_drop_in_exists_and_overrides_dynamic_user(self):
        self.assertTrue(self.DROP_IN.is_file(), f"missing drop-in at {self.DROP_IN}")
        text = self.DROP_IN.read_text()
        # The drop-in must declare [Service] and bind the unit to a fixed
        # user so the migration path in systemd (DynamicUser=yes +
        # CacheDirectory=fwupdmgr) is skipped.
        self.assertIn("[Service]", text)
        self.assertIn("User=fwupd-refresh", text)
        self.assertIn("Group=fwupd-refresh", text)
        # DynamicUser=no is the load-bearing directive: systemd's
        # exec_directory_is_private() gates the pre-existing-public ->
        # /var/cache/private migration on context->dynamic_user alone,
        # not on whether User= is set (User= + DynamicUser=yes is a
        # legal systemd.exec(5) combination). Without DynamicUser=no the
        # migration code still runs and the unit still fails the same
        # way. Comments may still mention DynamicUser=yes for context,
        # but only directive lines (no leading '#') are checked here.
        directive_lines = [
            line for line in text.splitlines()
            if line and not line.lstrip().startswith("#")
        ]
        self.assertTrue(
            any(line.strip() == "DynamicUser=no" for line in directive_lines),
            "drop-in must set DynamicUser=no so the migration code is skipped",
        )

    def test_drop_in_documents_the_bug(self):
        text = self.DROP_IN.read_text()
        # The comment must cite the actual symptom so a future reader can
        # verify the override still addresses it.
        self.assertIn("CacheDirectory=fwupdmgr", text)
        self.assertIn("/var/cache/private/fwupdmgr", text)
        self.assertIn("DynamicUser=yes", text)
        self.assertIn("#385", text)

    def test_sysusers_entry_allocates_the_user(self):
        self.assertTrue(self.SYSUSERS.is_file(), f"missing sysusers fragment at {self.SYSUSERS}")
        text = self.SYSUSERS.read_text()
        # The active line must create the user with auto-allocated UID/GID.
        # systemd-sysusers.d format: 'TYPE NAME ID GECOS [HOME [SHELL]]'.
        # The GECOS field is quoted and may contain spaces, so split with
        # maxsplit=3 (keeps the rest of the line as a single field).
        active = [line for line in text.splitlines() if line and not line.lstrip().startswith("#")]
        self.assertEqual(len(active), 1, "sysusers fragment must have exactly one active line")
        fields = active[0].split(None, 3)
        self.assertEqual(fields[0], "u")
        self.assertEqual(fields[1], "fwupd-refresh")
        self.assertEqual(fields[2], "-", "UID/GID must be auto-allocated, not pinned")
        self.assertIn("Firmware", fields[3])
        # The fragment must reference the drop-in it serves so the pair
        # cannot be deleted independently without leaving an orphan line.
        self.assertIn("10-utah-fwupd-refresh-user.conf", text)
        self.assertIn("#385", text)

    def test_drop_in_and_sysusers_agree_on_user_name(self):
        # Catch the case where one file is renamed and the other is not.
        drop_in_user = None
        for line in self.DROP_IN.read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith("User="):
                drop_in_user = stripped.split("=", 1)[1].strip()
                break
        sysusers_lines = self.SYSUSERS.read_text().splitlines()
        active = [line for line in sysusers_lines if line and not line.lstrip().startswith("#")]
        self.assertEqual(drop_in_user, active[0].split(None, 3)[1])

    def test_drop_in_reasserts_dynamic_user_hardening(self):
        # DynamicUser=yes implies NoNewPrivileges=yes, PrivateTmp=yes,
        # RemoveIPC=yes, RestrictSUIDSGID=yes, and ProtectSystem=strict
        # (systemd.exec(5); upstream's static-user branch in
        # data/motd/meson.build bumps ProtectSystem=strict). DynamicUser=no
        # does not imply them, so the drop-in must set them explicitly as
        # directive lines (comments don't count) to preserve the same
        # hardening posture.
        text = self.DROP_IN.read_text()
        directive_lines = [
            line.strip() for line in text.splitlines()
            if line and not line.lstrip().startswith("#")
        ]
        for required in (
            "DynamicUser=no",
            "NoNewPrivileges=yes",
            "PrivateTmp=yes",
            "RemoveIPC=yes",
            "RestrictSUIDSGID=yes",
            "ProtectSystem=strict",
        ):
            self.assertIn(
                required, directive_lines,
                f"drop-in must set {required} so the hardening posture is preserved",
            )


if __name__ == "__main__":
    unittest.main()
