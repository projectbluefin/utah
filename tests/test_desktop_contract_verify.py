"""Executed coverage for verify-desktop-contract.py's in-image verify mode.

`tests/test_desktop_contract.py` covers the helpers and `--check`. The verify
branch of `main()` -- everything after the `--check` early return -- had no
executed coverage at all. It runs exactly once, inside the Containerfile
against a composed image, and only ever on the path where the contract holds:
if any assertion in it silently stopped rejecting things, the build would still
go green and ship an image that does not match the contract.

These tests run that branch against a synthetic image root. `main()` reaches
the image through module-level `Path(...)` calls on absolute paths, so the
tests redirect the three image prefixes (`/usr`, `/etc`, `/var`) into a
temporary directory and leave every other path -- notably the contract file
itself -- alone. `unit_enabled` and `unit_masked` shell out to systemctl and
are covered by their own tests, so they are replaced here with declared sets.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_desktop_contract import ROOT, load

desktop = load("verify-desktop-contract")

REDIRECTED_PREFIXES = ("/usr", "/etc", "/var")

OS_RELEASE = {
    "NAME": "Utah",
    "ID": "utah",
    "PRETTY_NAME": "Utah (Version: 44)",
}
OS_RELEASE_PATTERNS = {"PRETTY_NAME": r"^Utah \(Version: .+\)$"}
IMAGE_INFO = {"image-name": "utah", "image-flavor": "main"}
APPS = ["org.gnome.Calculator", "org.mozilla.firefox"]
BREWFILE = "/usr/share/ublue-os/system-flatpaks.Brewfile"
REMOTE = "/etc/flatpak/remotes.d/flathub.flatpakrepo"
REMOTE_URL = "https://dl.flathub.org/repo/"


def image_path_factory(root):
    """Return a `Path` stand-in that redirects image paths under `root`."""

    def factory(*parts):
        path = Path(*parts)
        text = str(path)
        for prefix in REDIRECTED_PREFIXES:
            if text == prefix or text.startswith(prefix + "/"):
                return root / text.lstrip("/")
        return path

    return factory


def toml_value(value):
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, list):
        return "[" + ", ".join(json.dumps(item) for item in value) + "]"
    raise TypeError(value)


def to_toml(contract):
    """Serialize the small dict shapes these tests use into TOML text."""
    lines = []
    for section, body in contract.items():
        lines.append(f"[{section}]")
        tables = {}
        for key, value in body.items():
            if isinstance(value, dict):
                tables[key] = value
            else:
                lines.append(f"{json.dumps(key)} = {toml_value(value)}")
        for name, table in tables.items():
            lines.append(f"[{section}.{name}]")
            for key, value in table.items():
                lines.append(f"{json.dumps(key)} = {toml_value(value)}")
    return "\n".join(lines) + "\n"


def base_contract():
    return {
        "branding": {
            "files": ["/usr/share/ublue-os/bluefin.png"],
            "os_release": dict(OS_RELEASE),
            "os_release_patterns": dict(OS_RELEASE_PATTERNS),
            "image_info": dict(IMAGE_INFO),
            "image_info_patterns": {"image-flavor": r"^(main|nvidia)$"},
        },
        "configuration": {
            "files": ["/etc/dconf/db/distro.d/01-bluefin-folders"],
            "file_contains": {"/etc/dconf/db/distro.d/01-bluefin-folders": ["Bazaar"]},
        },
        "flatpak": {
            "files": [REMOTE],
            "brewfile": BREWFILE,
            "remote": REMOTE,
            "remote_url": REMOTE_URL,
            "apps": list(APPS),
        },
        "services": {"enabled": ["gdm.service"], "masked": ["bootc-fetch-apply-updates.timer"]},
    }


class VerifyModeTests(unittest.TestCase):
    """Run `main()` without `--check` against a synthetic composed image."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "image"
        self.write("/usr/lib/os-release", self.os_release_text(OS_RELEASE))
        self.write("/usr/share/ublue-os/image-info.json", json.dumps(IMAGE_INFO))
        self.write("/usr/share/ublue-os/bluefin.png", "png")
        self.write("/etc/dconf/db/distro.d/01-bluefin-folders", "Bazaar App Store\n")
        self.write(BREWFILE, "".join(f'flatpak "{app}"\n' for app in APPS))
        self.write(REMOTE, f"[Flatpak Remote]\nUrl={REMOTE_URL}\n")
        self.enabled = {"gdm.service"}
        self.user_enabled = {"pipewire.socket", "wireplumber.service"}
        self.masked = {"bootc-fetch-apply-updates.timer"}

    @staticmethod
    def os_release_text(values):
        return "".join(f'{key}="{value}"\n' for key, value in values.items())

    def write(self, image_path, text):
        path = self.root / image_path.lstrip("/")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def remove(self, image_path):
        (self.root / image_path.lstrip("/")).unlink()

    def verify(self, contract=None):
        """Return (exit code, stdout, stderr) for one verify-mode run."""
        contract = base_contract() if contract is None else contract
        path = Path(self.tmp.name) / "contract.toml"
        path.write_text(to_toml(contract))
        with patch.object(desktop, "Path", image_path_factory(self.root)), patch.object(
            desktop, "unit_enabled", lambda unit: unit in self.enabled
        ), patch.object(
            desktop, "user_unit_enabled", lambda unit: unit in self.user_enabled
        ), patch.object(desktop, "unit_masked", lambda unit: unit in self.masked), patch.object(
            desktop.sys, "argv", ["verify", str(path)]
        ):
            with patch("sys.stdout") as out, patch.object(desktop.sys, "stderr") as err:
                code = desktop.main()
        joined = lambda stream: "".join(
            str(call.args[0]) for call in stream.write.call_args_list if call.args
        )
        return code, joined(out), joined(err)

    def assert_rejected(self, contract=None, *, naming):
        code, _, errors = self.verify(contract)
        self.assertEqual(code, 1, f"verifier accepted the image; stderr was {errors!r}")
        self.assertIn(naming, errors)
        return errors


class UserServiceTests(VerifyModeTests):
    """Hummingbird's user preset disables every per-user service but dbus, so
    an installed Utah had no audio server. The contract's user_enabled list
    is checked with systemctl --global."""

    def contract_with_user_units(self, *units):
        contract = base_contract()
        contract["services"] = dict(contract["services"], user_enabled=list(units))
        return contract

    def test_globally_enabled_user_services_pass_and_are_counted(self):
        code, out, errors = self.verify(self.contract_with_user_units("pipewire.socket", "wireplumber.service"))
        self.assertEqual(code, 0, errors)
        self.assertIn("2 user services", out)

    def test_a_disabled_audio_service_is_rejected_by_name(self):
        self.user_enabled.discard("wireplumber.service")
        self.assert_rejected(
            self.contract_with_user_units("pipewire.socket", "wireplumber.service"),
            naming="required user service is not enabled globally: wireplumber.service",
        )


class CompliantImageTests(VerifyModeTests):
    def test_a_compliant_image_passes_and_reports_its_counts(self):
        code, out, errors = self.verify()
        self.assertEqual(code, 0, errors)
        self.assertIn("Utah desktop contract passed", out)
        self.assertIn(f"{len(APPS)} Flatpaks", out)
        self.assertIn("1 enabled services", out)
        self.assertIn("1 masked services", out)

    def test_the_masked_count_is_omitted_when_nothing_is_masked(self):
        contract = base_contract()
        contract["services"] = {"enabled": ["gdm.service"]}
        code, out, errors = self.verify(contract)
        self.assertEqual(code, 0, errors)
        self.assertNotIn("masked services", out)

    def test_verify_mode_reads_the_image_not_the_build_tree(self):
        self.remove("/usr/lib/os-release")
        with self.assertRaises(FileNotFoundError):
            self.verify()


class RequiredFileTests(VerifyModeTests):
    def test_a_missing_branding_file_is_named(self):
        self.remove("/usr/share/ublue-os/bluefin.png")
        self.assert_rejected(naming="required file is missing: /usr/share/ublue-os/bluefin.png")

    def test_a_missing_flatpak_file_is_named(self):
        self.remove(REMOTE)
        self.assert_rejected(naming=f"required file is missing: {REMOTE}")

    def test_a_declared_directory_does_not_satisfy_a_required_file(self):
        self.remove("/usr/share/ublue-os/bluefin.png")
        (self.root / "usr/share/ublue-os/bluefin.png").mkdir(parents=True)
        self.assert_rejected(naming="required file is missing: /usr/share/ublue-os/bluefin.png")


class OsReleaseTests(VerifyModeTests):
    def test_a_wrong_os_release_value_is_reported(self):
        values = dict(OS_RELEASE, ID="bluefin")
        self.write("/usr/lib/os-release", self.os_release_text(values))
        self.assert_rejected(naming="os-release ID must be 'utah', got 'bluefin'")

    def test_an_os_release_pattern_must_match(self):
        values = dict(OS_RELEASE, PRETTY_NAME="Utah")
        self.write("/usr/lib/os-release", self.os_release_text(values))
        self.assert_rejected(naming="os-release PRETTY_NAME must match")

    def test_an_absent_os_release_key_is_reported_not_skipped(self):
        values = {key: value for key, value in OS_RELEASE.items() if key != "ID"}
        self.write("/usr/lib/os-release", self.os_release_text(values))
        self.assert_rejected(naming="os-release ID must be 'utah', got None")


class ImageInfoTests(VerifyModeTests):
    def test_a_wrong_image_info_value_is_reported(self):
        self.write(
            "/usr/share/ublue-os/image-info.json",
            json.dumps(dict(IMAGE_INFO, **{"image-name": "bluefin"})),
        )
        self.assert_rejected(naming="image-info image-name must be 'utah', got 'bluefin'")

    def test_an_image_info_pattern_must_match(self):
        self.write(
            "/usr/share/ublue-os/image-info.json",
            json.dumps(dict(IMAGE_INFO, **{"image-flavor": "gaming"})),
        )
        self.assert_rejected(naming="image-info image-flavor must match")

    def test_unparseable_image_info_is_reported_rather_than_raising(self):
        self.write("/usr/share/ublue-os/image-info.json", "{not json")
        self.assert_rejected(naming="invalid image-info.json")

    def test_an_absent_image_info_is_tolerated(self):
        # Documented, deliberate: image-info.json is not in branding.files, so
        # an image that never wrote one is accepted and its branding assertions
        # are skipped entirely. Change the contract, not this test, to gate it.
        self.remove("/usr/share/ublue-os/image-info.json")
        code, _, errors = self.verify()
        self.assertEqual(code, 0, errors)


class FileContainsTests(VerifyModeTests):
    def test_a_missing_required_setting_is_named_with_its_file(self):
        self.write("/etc/dconf/db/distro.d/01-bluefin-folders", "nothing useful\n")
        self.assert_rejected(
            naming="/etc/dconf/db/distro.d/01-bluefin-folders is missing required setting: 'Bazaar'"
        )

    def test_every_missing_setting_is_reported_not_only_the_first(self):
        contract = base_contract()
        contract["configuration"]["file_contains"] = {
            "/etc/dconf/db/distro.d/01-bluefin-folders": ["Bazaar", "Ptyxis"]
        }
        self.write("/etc/dconf/db/distro.d/01-bluefin-folders", "nothing useful\n")
        errors = self.assert_rejected(contract, naming="'Bazaar'")
        self.assertIn("'Ptyxis'", errors)

    def test_file_contains_is_skipped_for_a_file_absent_from_the_image(self):
        contract = base_contract()
        contract["configuration"]["files"] = []
        contract["configuration"]["file_contains"] = {"/etc/absent": ["anything"]}
        code, _, errors = self.verify(contract)
        self.assertEqual(code, 0, errors)


class BrewfileTests(VerifyModeTests):
    def test_a_missing_flatpak_is_named_as_missing(self):
        self.write(BREWFILE, 'flatpak "org.gnome.Calculator"\n')
        errors = self.assert_rejected(naming="Flatpak Brewfile differs from the contract")
        self.assertIn("missing: org.mozilla.firefox", errors)
        self.assertIn("extra: none", errors)

    def test_an_unexpected_flatpak_is_named_as_extra(self):
        self.write(BREWFILE, "".join(f'flatpak "{app}"\n' for app in APPS + ["com.example.Rogue"]))
        errors = self.assert_rejected(naming="Flatpak Brewfile differs from the contract")
        self.assertIn("extra: com.example.Rogue", errors)
        self.assertIn("missing: none", errors)

    def test_brewfile_comparison_is_order_sensitive(self):
        self.write(BREWFILE, "".join(f'flatpak "{app}"\n' for app in reversed(APPS)))
        errors = self.assert_rejected(naming="Flatpak Brewfile differs from the contract")
        self.assertIn("missing: none", errors)
        self.assertIn("extra: none", errors)

    def test_an_absent_brewfile_is_skipped(self):
        contract = base_contract()
        contract["flatpak"]["brewfile"] = "/usr/share/ublue-os/absent.Brewfile"
        code, _, errors = self.verify(contract)
        self.assertEqual(code, 0, errors)


class RemoteTests(VerifyModeTests):
    def test_a_remote_pointing_elsewhere_is_rejected(self):
        self.write(REMOTE, "[Flatpak Remote]\nUrl=https://example.invalid/repo/\n")
        self.assert_rejected(naming=f"does not configure {REMOTE_URL}")

    def test_an_absent_remote_file_is_skipped(self):
        contract = base_contract()
        contract["flatpak"]["files"] = []
        contract["flatpak"]["remote"] = "/etc/flatpak/remotes.d/absent.flatpakrepo"
        code, _, errors = self.verify(contract)
        self.assertEqual(code, 0, errors)


class ServiceTests(VerifyModeTests):
    def test_a_service_that_is_not_enabled_is_named(self):
        self.enabled = set()
        self.assert_rejected(naming="required service is not enabled: gdm.service")

    def test_a_service_that_is_not_masked_is_named(self):
        self.masked = set()
        self.assert_rejected(
            naming="required service is not masked: bootc-fetch-apply-updates.timer"
        )

    def test_every_unsatisfied_unit_is_reported(self):
        contract = base_contract()
        contract["services"] = {"enabled": ["gdm.service", "sshd.service"], "masked": []}
        self.enabled = set()
        errors = self.assert_rejected(contract, naming="not enabled: gdm.service")
        self.assertIn("not enabled: sshd.service", errors)


class FailClosedTests(VerifyModeTests):
    def test_unrelated_failures_are_all_reported_in_one_run(self):
        self.remove("/usr/share/ublue-os/bluefin.png")
        self.write("/usr/lib/os-release", self.os_release_text(dict(OS_RELEASE, ID="bluefin")))
        self.enabled = set()
        errors = self.assert_rejected(naming="required file is missing")
        self.assertIn("os-release ID must be", errors)
        self.assertIn("required service is not enabled", errors)

    def test_a_failing_run_prints_no_success_summary(self):
        self.enabled = set()
        code, out, _ = self.verify()
        self.assertEqual(code, 1)
        self.assertNotIn("contract passed", out)

    def test_the_shipped_contract_is_rejected_against_an_empty_image(self):
        # The shipped contract is a real input; an image with none of it must
        # not pass. This is the assertion the Containerfile relies on.
        contract_text = (ROOT / "contracts/bluefin-desktop.toml").read_text()
        path = Path(self.tmp.name) / "shipped.toml"
        path.write_text(contract_text)
        self.write("/usr/lib/os-release", 'NAME="Not Utah"\n')
        self.enabled = set()
        self.masked = set()
        with patch.object(desktop, "Path", image_path_factory(self.root)), patch.object(
            desktop, "unit_enabled", lambda unit: False
        ), patch.object(desktop, "unit_masked", lambda unit: False), patch.object(
            desktop.sys, "argv", ["verify", str(path)]
        ):
            with patch("sys.stdout"), patch.object(desktop.sys, "stderr"):
                self.assertEqual(desktop.main(), 1)


if __name__ == "__main__":
    unittest.main()
