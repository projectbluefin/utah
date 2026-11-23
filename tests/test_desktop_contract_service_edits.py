"""Coverage for the edits Utah makes to shipped service and Common files.

Split out of tests/test_desktop_contract.py (#1373): the fwupd-refresh.service
drop-in and its static user, and Dakota's edits to Common's files applied by
configure-services.sh.
"""

import tomllib
import tempfile
import unittest
from pathlib import Path

from test_desktop_contract import ROOT
from test_desktop_contract_services import preset_verdict


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


class DakotaCommonEditTests(unittest.TestCase):
    """Dakota's edits to common's files, applied by configure-services.sh (#1266).

    Each edit must be idempotent and fail the build when common's file no
    longer has the shape it was written against.
    """

    SERVICES = (ROOT / "scripts/configure-services.sh").read_text()
    COMMON_STATS = """    "break",
    {
      "type": "command",
      "text": "cat /usr/share/ublue-os/fastfetch-user-count",
      "format": "Murder Chickens: {1} (weekly)"
    },
    {
      "type": "command",
      "text": "cat /usr/share/ublue-os/bazaar-install-count",
      "format": "Bazaar Installs: {1} (weekly)"
    },
    "break",
    {
      "type": "host"
    }
"""
    DAKOTA_STATS = """    "break",
    {
      "type": "host"
    }
"""

    def block(self, start):
        import re

        return re.search(rf"^{re.escape(start)}.*?^fi$", self.SERVICES, re.S | re.M).group(0)

    def run_block(self, block, env_line):
        import subprocess

        return subprocess.run(
            ["bash", "-euo", "pipefail", "-c", f"{env_line}\n{block}\n"],
            capture_output=True, text=True,
        )

    def test_weekly_stats_drop_matches_dakota_patch_once(self):
        block = self.block("# Weekly community stats")
        with tempfile.TemporaryDirectory() as tmp:
            jsonc = Path(tmp) / "fastfetch.jsonc"
            jsonc.write_text(self.COMMON_STATS)
            for _ in range(2):
                result = self.run_block(block, f"FASTFETCH_JSONC={jsonc}")
                self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(jsonc.read_text(), self.DAKOTA_STATS)

            jsonc.write_text('      "text": "cat /usr/share/ublue-os/fastfetch-user-count",\n')
            result = self.run_block(block, f"FASTFETCH_JSONC={jsonc}")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("re-fit Dakota's weekly-stats drop", result.stderr)

    def test_ghostty_skel_follows_light_and_dark_once(self):
        block = self.block("GHOSTTY_SKEL=")
        with tempfile.TemporaryDirectory() as tmp:
            skel = Path(tmp) / "config.ghostty"
            block = block.replace("/etc/skel/.config/ghostty/config.ghostty", str(skel))
            skel.write_text("# comment\ntheme = Catppuccin Mocha\nwindow-theme = ghostty\n")
            for _ in range(2):
                result = self.run_block(block, "")
                self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                skel.read_text(),
                "# comment\ntheme = light:Catppuccin Latte,dark:Catppuccin Mocha\nwindow-theme = ghostty\n",
            )

            skel.write_text("theme = Dracula\n")
            result = self.run_block(block, "")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("re-fit Dakota's dual-theme patch", result.stderr)

    def test_zz0_check_fails_when_dakotas_repoint_would_apply(self):
        block = self.block("ZZ0_OVERRIDE=")
        with tempfile.TemporaryDirectory() as tmp:
            override = Path(tmp) / "zz0.override"
            block = block.replace(
                "/usr/share/glib-2.0/schemas/zz0-bluefin-modifications.gschema.override", str(override)
            )
            uri = "picture-uri='file:///usr/share/backgrounds/bluefin/{}-bluefin.xml'\n"
            override.write_text(uri.format("06"))
            self.assertEqual(self.run_block(block, "").returncode, 0)
            override.write_text(uri.format("12"))
            result = self.run_block(block, "")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("projectbluefin/dakota#1902", result.stderr)
            override.write_text("picture-uri='file:///var/lib/ublue-os/current-bluefin.xml'\n")
            self.assertNotEqual(self.run_block(block, "").returncode, 0)

    def test_month_units_ship_as_dakota_and_are_enabled(self):
        import os

        contract = tomllib.loads((ROOT / "contracts/bluefin-desktop.toml").read_text())
        for unit in ("ublue-wallpaper-month.service", "bluefin-wallpaper-month.service"):
            with self.subTest(unit=unit):
                self.assertTrue((ROOT / "system_files/shared/usr/lib/systemd/system" / unit).is_file())
                self.assertIn(unit, contract["services"]["enabled"])
                self.assertEqual(preset_verdict(unit)[0], "enable")
        sync = ROOT / "system_files/shared/usr/libexec/bluefin-wallpaper-month-sync"
        self.assertTrue(os.access(sync, os.X_OK))

    def test_passwd_stack_switches_keyring_hook_to_pam_oo7_once(self):
        # Hummingbird's shadow-utils /etc/pam.d/passwd; Dakota's
        # bluefin/oo7-passwd-pam.bst adds the same hook after the substack (#1227).
        block = self.block("PASSWD_PAM=")
        hummingbird = (
            "#%PAM-1.0\n# This tool only uses the password stack.\n"
            "password   substack\tsystem-auth\n"
            "-password   optional\tpam_gnome_keyring.so use_authtok\n"
            "password   substack\tpostlogin\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            passwd = Path(tmp) / "passwd"
            block = block.replace("/etc/pam.d/passwd", str(passwd))
            passwd.write_text(hummingbird)
            for _ in range(2):
                result = self.run_block(block, "")
                self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(passwd.read_text(), hummingbird.replace("pam_gnome_keyring", "pam_oo7"))

            passwd.write_text("password include system-auth\n")
            result = self.run_block(block, "")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("re-fit the pam_oo7 password hook", result.stderr)


if __name__ == "__main__":
    unittest.main()
