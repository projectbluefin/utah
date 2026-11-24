"""Service preset coverage for the desktop contract.

Split out of tests/test_desktop_contract.py (#1373). ServiceMaskParityTests
checks that Utah's systemd presets enable, disable and mask the units the
contract names, as Dakota does.
"""

import fnmatch
import tomllib
import unittest
from pathlib import Path

from test_desktop_contract import ROOT


SYSTEM_PRESETS = ROOT / "system_files/shared/usr/lib/systemd/system-preset"


def preset_verdict(unit):
    """The first preset line matching `unit` across Utah's system presets.

    systemd reads preset files in lexical order of their names and the first
    matching line wins. Hummingbird's own 80-/90- files are not in the tree;
    85-utah-desktop.preset sorts before them for the units they name.
    """
    for path in sorted(SYSTEM_PRESETS.glob("*.preset"), key=lambda p: p.name):
        for line in path.read_text().splitlines():
            words = line.split()
            if len(words) >= 2 and words[0] in ("enable", "disable") and fnmatch.fnmatchcase(unit, words[1]):
                return words[0], path.name
    return None, None


class ServiceMaskParityTests(unittest.TestCase):
    """Utah's services follow its presets, enabled by default as on Dakota (#1233)."""

    def contract_services(self):
        return tomllib.loads((ROOT / "contracts/bluefin-desktop.toml").read_text())["services"]

    def test_enable_by_default_outranks_hummingbirds_catch_all_disable(self):
        # Hummingbird ships 99-default-disable.preset ("disable *"); a line
        # that matches first in a lower-sorting file wins, so 98 must be the
        # last Utah file and must end on the catch-all.
        names = sorted(p.name for p in SYSTEM_PRESETS.glob("*.preset"))
        self.assertEqual(names[-1], "98-utah-enable-default.preset")
        self.assertLess(names[-1], "99-default-disable.preset")
        lines = [
            line for line in (SYSTEM_PRESETS / names[-1]).read_text().splitlines()
            if line.strip() and not line.startswith("#")
        ]
        self.assertEqual(lines[-1], "enable *")
        self.assertEqual([line for line in lines if line.split()[-1] == "*"], ["enable *"])

    def test_contract_enabled_units_resolve_to_enabled(self):
        for unit in self.contract_services()["enabled"]:
            with self.subTest(unit=unit):
                self.assertEqual(preset_verdict(unit)[0], "enable")

    def test_contract_disabled_and_masked_units_resolve_to_disabled(self):
        services = self.contract_services()
        for unit in services["disabled"] + services["masked"]:
            with self.subTest(unit=unit):
                self.assertEqual(preset_verdict(unit)[0], "disable")

    def test_configure_services_applies_the_presets(self):
        # The build applies the same presets first boot would, instead of a
        # per-unit enable list that can drift from them (#98, #1233).
        services = (ROOT / "scripts/configure-services.sh").read_text()
        self.assertRegex(services, r"(?m)^systemctl preset-all$")
        self.assertNotRegex(services, r"(?m)^\s*systemctl (enable|disable) ")
        self.assertNotIn("enable_unit", services)
        uupd = services.index("mv -f /etc/uupd/config.json.rpmnew /etc/uupd/config.json")
        self.assertLess(uupd, services.index("\nsystemctl preset-all\n"))

    def test_the_reported_desktop_units_are_enabled(self):
        # projectbluefin/utah#99: an installed unit that never started. #19: the
        # first-boot Flatpak, setup, update and Tailscale units need an
        # enablement policy too. #675, #1265: Dakota's first-boot date and
        # booted-image units. #1266: Dakota's monthly-wallpaper units.
        enabled = self.contract_services()["enabled"]
        for unit in (
            "gdm.service",
            "ublue-wallpaper-month.service",
            "flatpak-preinstall.service",
            "ublue-system-setup.service",
            "bluefin-wallpaper-month.service",
            "projectbluefin-countme.timer",
            "uupd.timer",
            "accounts-daemon.service",
            "ublue-firstboot-date.path",
            "ublue-booted-image.service",
            "bluefin-remove-installer.service",
        ):
            with self.subTest(unit=unit):
                self.assertIn(unit, enabled)
                self.assertEqual(preset_verdict(unit)[0], "enable")
        # colord.service has no [Install] section in the factory build, so
        # the contract cannot require it enabled; the preset still turns it on.
        self.assertNotIn("colord.service", enabled)
        self.assertEqual(preset_verdict("colord.service")[0], "enable")
        # dakota-config-firstboot-date ships these units (#1265); Utah keeps no copy.
        parity = tomllib.loads((ROOT / "packages/utah.toml").read_text())["parity"]["packages"]
        self.assertIn("dakota-config-firstboot-date", parity)
        for unit in ("ublue-firstboot-date.path", "ublue-booted-image.service",
                     "bluefin-remove-installer.service"):
            self.assertFalse((ROOT / "system_files/shared/usr/lib/systemd/system" / unit).exists())

    def test_dakota_gnome_user_units_enabled_in_contract_preset_and_script(self):
        # Dakota inherits these from gnome-build-meta's user presets.
        contract = tomllib.loads((ROOT / "contracts/bluefin-desktop.toml").read_text())
        preset = (ROOT / "system_files/shared/usr/lib/systemd/user-preset/85-utah-desktop.preset").read_text()
        services = (ROOT / "scripts/configure-services.sh").read_text()
        user_loop = services.split("for unit in pipewire.socket", 1)[1].split("; do", 1)[0]
        for unit in ("speech-dispatcher.socket", "xdg-desktop-portal-rewrite-launchers.service"):
            with self.subTest(unit=unit):
                self.assertIn(unit, contract["services"]["user_enabled"])
                self.assertIn(f"enable {unit}\n", preset)
                self.assertIn(unit, user_loop)

    def test_dakota_and_gnome_os_disables_are_carried(self):
        # GNOME OS's openssh/wpa-supplicant/80-network presets and Dakota's
        # 50-*/80-* presets (sysstat, libvirt, rpcbind, pcrlock, ...).
        for unit, verdict in (
            ("sshd.service", "disable"),
            ("systemd-networkd.service", "disable"),
            ("wpa_supplicant@.service", "disable"),
            ("iscsid.socket", "disable"),
            ("libvirtd.socket", "disable"),
            ("virtqemud.service", "disable"),
            ("nfs-server.service", "disable"),
            ("rpcbind.socket", "disable"),
            ("lm_sensors.service", "disable"),
            ("kanata.service", "disable"),
            ("kmscon.service", "disable"),
            ("systemd-pcrlock-firmware-code.service", "disable"),
            ("systemd-pcrlock-machine-id.service", "enable"),
            ("sysstat.service", "disable"),
            ("sysstat-collect.timer", "disable"),
            ("sysstat-summary.timer", "disable"),
            ("sysstat-rotate.timer", "disable"),
        ):
            with self.subTest(unit=unit):
                self.assertEqual(preset_verdict(unit)[0], verdict)

    def test_units_hummingbird_enables_are_disabled_before_its_presets(self):
        # Hummingbird's 90-default.preset enables these; only a line in a file
        # sorting before it can turn them off.
        for unit in (
            "sshd.service",
            "multipathd.service",
            "lm_sensors.service",
            "iscsid.socket",
            "iscsiuio.socket",
            "iscsi-starter.service",
            "iscsi-onboot.service",
        ):
            with self.subTest(unit=unit):
                self.assertEqual(preset_verdict(unit), ("disable", "85-utah-desktop.preset"))

    def test_iscsi_units_ship_disabled(self):
        # iscsi-initiator-utils and isns-utils are installed as on Dakota, with
        # every unit they ship off (#1360): Dakota's 50-iscsi.preset plus the
        # Fedora-only starter and onboot units Hummingbird enables.
        packages = tomllib.loads((ROOT / "packages/utah.toml").read_text())
        installed = packages["parity"]["packages"]
        disabled = self.contract_services()["disabled"]
        for package in ("iscsi-initiator-utils", "isns-utils"):
            with self.subTest(package=package):
                self.assertIn(package, installed)
        for unit in (
            "iscsi-init.service",
            "iscsi.service",
            "iscsi-starter.service",
            "iscsi-onboot.service",
            "iscsid.service",
            "iscsid.socket",
            "iscsiuio.service",
            "iscsiuio.socket",
            "isnsd.service",
        ):
            with self.subTest(unit=unit):
                self.assertEqual(preset_verdict(unit), ("disable", "85-utah-desktop.preset"))
                # iscsi.service's [Install] holds only Also=iscsi-starter.service,
                # so `systemctl is-enabled` reports "indirect" and the verifier
                # cannot assert it; iscsi-starter.service starts it at boot.
                if unit == "iscsi.service":
                    self.assertNotIn(unit, disabled)
                else:
                    self.assertIn(unit, disabled)
        # clean-stage strips /var; tmpfiles recreates the node database.
        tmpfiles = (ROOT / "system_files/shared/usr/lib/tmpfiles.d/utah-iscsi.conf").read_text()
        for path in ("/var/lib/iscsi/nodes", "/var/lib/iscsi/send_targets", "/var/lib/isns"):
            with self.subTest(path=path):
                self.assertRegex(tmpfiles, rf"(?m)^d {path} 07\d\d root root -$")

    def test_syncthing_ships_opt_in(self):
        # Native syncthing as on Dakota (#1361): installed, its user unit never
        # enabled for everyone, and the RPM's app-grid launchers removed.
        packages = tomllib.loads((ROOT / "packages/utah.toml").read_text())
        self.assertIn("syncthing", packages["parity"]["packages"])
        self.assertIn("syncthing.service", self.contract_services()["user_disabled"])
        user_preset = (ROOT / "system_files/shared/usr/lib/systemd/user-preset/85-utah-desktop.preset").read_text()
        self.assertNotIn("syncthing", user_preset)
        services = (ROOT / "scripts/configure-services.sh").read_text()
        self.assertNotIn("enable syncthing", services)
        self.assertIn(
            "rm -f /usr/share/applications/syncthing-start.desktop \\\n"
            "    /usr/share/applications/syncthing-ui.desktop\n",
            services,
        )

    def test_sysprofd_and_erofs_utils_ship_native(self):
        # Dakota ships Sysprof's host daemon and erofs-utils (#1361, #1360).
        # sysprof3.service has no [Install] section: D-Bus activates it when
        # the Flathub app asks, so no preset list may name it.
        packages = tomllib.loads((ROOT / "packages/utah.toml").read_text())
        installed = packages["parity"]["packages"]
        contract = tomllib.loads((ROOT / "contracts/bluefin-desktop.toml").read_text())
        files = contract["configuration"]["files"]
        for package, path in (
            ("sysprof-cli", "/usr/libexec/sysprofd"),
            ("sysprof-cli", "/usr/lib/systemd/system/sysprof3.service"),
            ("sysprof-cli", "/usr/share/dbus-1/system-services/org.gnome.Sysprof3.service"),
            ("erofs-utils", "/usr/bin/mkfs.erofs"),
        ):
            with self.subTest(path=path):
                self.assertIn(package, installed)
                self.assertIn(path, files)
        for listing, units in self.contract_services().items():
            with self.subTest(listing=listing):
                self.assertNotIn("sysprof3.service", units)

    def test_first_boot_cannot_reset_build_time_state(self):
        # authselect-first-boot.service re-selects @system-default on first
        # boot, dropping the features configure-services.sh enables; livesys
        # is live-media setup; dbus-daemon would take dbus-broker's alias.
        for unit in ("authselect-first-boot.service", "livesys.service", "dbus-daemon.service"):
            with self.subTest(unit=unit):
                self.assertEqual(preset_verdict(unit)[0], "disable")

    def test_dakota_upstream_preset_units_enabled_in_contract_and_presets(self):
        # Units upstream presets enable that a booted Dakota runs (#1271,
        # #1272); enable-by-default turns them on and the contract asserts it.
        enabled = self.contract_services()["enabled"]
        for unit in (
            "nvmf-autoconnect.service",
            "nvmefc-boot-connections.service",
            "systemd-imdsd.socket",
            "speech-dispatcherd.service",
            "podman.service",
            "netavark-dhcp-proxy.socket",
            "netavark-dhcp-proxy.service",
            "netavark-firewalld-reload.service",
            "netavark-nftables-reload.service",
        ):
            with self.subTest(unit=unit):
                self.assertIn(unit, enabled)
                self.assertEqual(preset_verdict(unit)[0], "enable")

    def test_mdadm_timers_stay_off_beside_raid_check(self):
        # raid-check.timer is the scrub scheduler (#1268); mdadm's own timers
        # would scrub the same arrays again (#1271).
        disabled = self.contract_services()["disabled"]
        for unit in ("mdcheck_start.timer", "mdcheck_continue.timer", "mdmonitor-oneshot.timer"):
            with self.subTest(unit=unit):
                self.assertIn(unit, disabled)
                self.assertEqual(preset_verdict(unit), ("disable", "98-utah-enable-default.preset"))

    def test_nfs_client_target_enabled_with_gssproxy_state_dirs(self):
        # Dakota enables nfs-client.target; on Utah it pulls in gssproxy, whose
        # StateDirectory= systemd cannot create under SELinux on the stripped
        # /var, so tmpfiles must ship the RPM's directories (#1269).
        # Hummingbird's 90-default.preset enables the target; no Utah preset
        # may turn it off first.
        self.assertIn("nfs-client.target", self.contract_services()["enabled"])
        self.assertEqual(preset_verdict("nfs-client.target"), ("enable", "98-utah-enable-default.preset"))
        tmpfiles = [
            line.split()
            for line in (ROOT / "system_files/shared/usr/lib/tmpfiles.d/utah-gssproxy.conf").read_text().splitlines()
            if line.strip() and not line.startswith("#")
        ]
        self.assertEqual(
            tmpfiles,
            [
                ["d", "/var/lib/gssproxy", "0755", "root", "root", "-"],
                ["d", "/var/lib/gssproxy/clients", "0700", "root", "root", "-"],
                ["d", "/var/lib/gssproxy/rcache", "0700", "root", "root", "-"],
            ],
        )

    def test_dakota_parity_patch_targets_are_required_files(self):
        contract = tomllib.loads((ROOT / "contracts/bluefin-desktop.toml").read_text())
        files = contract["configuration"]["files"]
        file_contains = contract["configuration"]["file_contains"]
        for path in (
            "/usr/share/ublue-os/fastfetch.jsonc",
            "/usr/bin/ublue-image-info.sh",
            "/usr/libexec/ublue-booted-image",
            "/etc/profile.d/uwelcome.sh",
        ):
            with self.subTest(path=path):
                self.assertIn(path, files)
                self.assertIn(path, file_contains)

    def test_bluefin_cli_shims_are_shipped_and_required(self):
        # bluefin-cli replaces Common's bling (uwelcome stays the greeting).
        # Every pinned string must be in the file Utah overlays, or the image
        # gate fails.
        contract = tomllib.loads((ROOT / "contracts/bluefin-desktop.toml").read_text())
        files = contract["configuration"]["files"]
        file_contains = contract["configuration"]["file_contains"]
        for path in (
            "/usr/share/ublue-os/bling/bling.sh",
            "/usr/share/ublue-os/bling/bling.fish",
            "/usr/bin/ublue-bling",
        ):
            with self.subTest(path=path):
                self.assertIn(path, files)
                shipped = (ROOT / "system_files/shared" / path.lstrip("/")).read_text()
                for needle in file_contains[path]:
                    self.assertIn(needle, shipped)

    def test_bling_helpers_only_common_bling_read_are_removed(self):
        services = (ROOT / "scripts/configure-services.sh").read_text()
        for path in (
            "/usr/share/ublue-os/bling/env.sh",
            "/usr/share/ublue-os/bling/bash-preexec-rearm.sh",
        ):
            with self.subTest(path=path):
                self.assertRegex(services, rf"rm -f [^\n]*(\\\n[^\n]*)*{path}")
        # uwelcome stays the terminal greeting.
        self.assertNotIn("/usr/bin/uwelcome", services)

    def test_dakota_uupd_files_come_from_the_factory_and_are_required(self):
        # Dakota's elements/bluefin/uupd.bst ships these beside the binary;
        # dakota-config-uupd packages them (#1265), so Utah keeps no copy.
        contract = tomllib.loads((ROOT / "contracts/bluefin-desktop.toml").read_text())
        files = contract["configuration"]["files"]
        file_contains = contract["configuration"]["file_contains"]
        manifest = tomllib.loads((ROOT / "packages/utah.toml").read_text())["parity"]["packages"]
        self.assertIn("dakota-config-uupd", manifest)
        for path in (
            "/usr/lib/systemd/system/uupd-manual.service",
            "/etc/polkit-1/rules.d/uupd.rules",
            "/etc/uupd/config.json",
        ):
            with self.subTest(path=path):
                self.assertIn(path, files)
                self.assertFalse((ROOT / "system_files/shared" / path.lstrip("/")).exists())
        # The package's units replace Common's copies; pin which one ships.
        for path in ("/usr/lib/systemd/system/uupd.timer", "/usr/lib/systemd/system/uupd.service",
                     "/etc/uupd/config.json"):
            with self.subTest(path=path):
                self.assertIn(path, files)
                self.assertIn(path, file_contains)

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

    def test_desktop_contract_holds_httpd_disabled(self):
        # Apache httpd ships via gnome-user-share's WebDAV but the system units
        # must never run; #948 asserts they stay off in the contract so a future
        # enablement fails image verification instead of landing undetected.
        import tomllib

        contract = tomllib.loads((ROOT / "contracts/bluefin-desktop.toml").read_text())
        disabled = contract.get("services", {}).get("disabled", [])
        for unit in (
            "httpd.service",
            "httpd.socket",
            "httpd@.service",
            "htcacheclean.service",
        ):
            self.assertIn(unit, disabled, f"{unit} must be held disabled")

    def test_httpd_disabled_units_named_in_skill_doc(self):
        # A disabled unit added to the contract without the doc following leaves
        # the assertion unexplained; the reason travels with the #948 note.
        skill_doc = (ROOT / "docs/skills/desktop-contract.md").read_text()
        for unit in ("httpd.service", "httpd.socket", "httpd@.service"):
            self.assertIn(unit, skill_doc, f"{unit} must be named in the skill doc")
        self.assertIn("#948", skill_doc)

    def test_desktop_contract_services_listed_in_skill_doc(self):
        # The [services] section of the skill doc enumerates the units the
        # contract asserts; if a new unit is added to contracts/bluefin-desktop.toml
        # without the doc following, the verifier still passes and the drift only
        # shows up in review (#597). Assert each enabled unit is named in the doc.
        import tomllib

        contract = tomllib.loads((ROOT / "contracts/bluefin-desktop.toml").read_text())
        enabled = contract.get("services", {}).get("enabled", [])
        self.assertGreater(len(enabled), 0, "contract must list at least one enabled service")

        skill_doc = (ROOT / "docs/skills/desktop-contract.md").read_text()
        services_heading = skill_doc.split("`[services]`", 1)[1].split("\n\n", 1)[0]
        # The bullet links the Dakota service-state reference, which carries
        # the long list of units Dakota's enable-by-default policy turns on.
        services_heading += (
            ROOT / "docs/skills/desktop-contract/references/dakota-services.md"
        ).read_text()
        missing = [unit for unit in enabled if unit not in services_heading]
        self.assertEqual(
            missing, [],
            "every enabled unit must be named in docs/skills/desktop-contract.md "
            f"under the [services] bullet; missing: {missing}",
        )

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


if __name__ == "__main__":
    unittest.main()
