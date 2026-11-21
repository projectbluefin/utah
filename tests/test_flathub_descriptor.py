"""Run the service script's real integrity check without network or host writes."""

import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DESCRIPTOR = ROOT / "tests/fixtures/flathub.flatpakrepo"


class FlathubDescriptorIntegrityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="flathub-descriptor-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.original = b"existing remote\n"
        self.destination = self.root / "etc/flatpak/remotes.d/flathub.flatpakrepo"
        self.destination.parent.mkdir(parents=True)
        self.destination.write_bytes(self.original)
        self.input = self.root / "input.flatpakrepo"
        self.input.write_bytes(DESCRIPTOR.read_bytes())
        units = self.root / "usr/lib/systemd/system"
        units.mkdir(parents=True)
        (units / "systemd-resolved.service").write_text("[Service]\nPrivateTmp=yes\n")
        # ChairLift preinstallation is now required desktop setup, even when
        # this fixture is exercising the independent Flatpak descriptor path.
        user_units = self.root / "usr/lib/systemd/user"
        user_units.mkdir(parents=True)
        (user_units / "brew-preinstall.service").write_text("[Service]\nExecStart=/usr/bin/brew-preinstall\n")
        # oo7-daemon is the required Secret Service (#1227).
        (user_units / "oo7-daemon.service").write_text("[Service]\nExecStart=/usr/libexec/oo7-daemon\n")
        # GTK3 apps follow the light/dark style (#1342); Utah ships the unit.
        (user_units / "ublue-gtk3-theme-sync.service").write_text("[Service]\nExecStart=/usr/libexec/ublue-gtk3-theme-sync\n")
        # Hummingbird's firewalld: firewalld.conf is a symlink to the
        # standard variant; gnome-os-config-firewalld adds the lax zone.
        zones = self.root / "usr/lib/firewalld/zones"
        zones.mkdir(parents=True)
        (zones / "lax.xml").write_text("<zone/>\n")
        firewalld = self.root / "etc/firewalld"
        firewalld.mkdir(parents=True)
        (firewalld / "firewalld-standard.conf").write_text(
            "DefaultZone=public\nIPv6_rpfilter=strict\nFirewallBackend=nftables\n"
        )
        (firewalld / "firewalld.conf").symlink_to("firewalld-standard.conf")
        uupd = self.root / "tmp/uupd"
        uupd.mkdir(parents=True)
        (uupd / "uupd").write_text("#!/bin/sh\nexit 0\n")
        # uutils sits beside GNU coreutils, whose cp bootupd needs (#1431);
        # sudo-rs and C sudo's sudoedit link arrive with their packages (#1229).
        bindir = self.root / "usr/bin"
        bindir.mkdir(parents=True)
        (bindir / "uutils-coreutils").write_text("#!/bin/sh\n")
        (bindir / "cp").write_text("GNU cp\n")
        (bindir / "sudo").write_text("C sudo\n")
        (bindir / "sudoedit").symlink_to("sudo")
        for name in ("sudo-rs", "su-rs"):
            (bindir / name).write_text(f"{name}\n")
            (bindir / name).chmod(0o4755)

    def run_script(self, **environment):
        source = (ROOT / "scripts/configure-services.sh").read_text()
        # Redirect image paths once; preserve real hashing/install/remove commands.
        source = re.sub(r"/(?:usr|etc|tmp)(?=/)",
                        lambda match: f"{self.root}{match.group()}", source)
        transport_and_services = '''\
systemctl() { return 0; }
authselect() { return 0; }
semodule() { return 0; }
matchpathcon() {
    if [[ "$1" == -n ]]; then printf '%s\\n' "$SUDO_LABEL"; return; fi
    echo "$1 system_u:object_r:swapfile_t:s0"
}
dnf5() { return 0; }
rpm() { return 1; }
curl() {
    while (( $# )); do
        if [[ "$1" == --output ]]; then
            cp "$DESCRIPTOR_INPUT" "$2"
            return
        fi
        shift
    done
    return 2
}
'''
        script = self.root / "configure-services.sh"
        script.write_text(transport_and_services + source)
        return subprocess.run(
            ["bash", str(script)], capture_output=True, text=True,
            env={**os.environ, "ENABLE_SSHD": "0", "DESCRIPTOR_INPUT": str(self.input),
                 "SUDO_LABEL": "system_u:object_r:sudo_exec_t:s0",
                 **environment},
        )

    def test_pinned_descriptor_is_installed_and_temporary_download_removed(self):
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        # The installed descriptor is the pinned one plus exactly one line:
        # the collection ID flatpak preinstall needs to match common's
        # CollectionID=org.flathub.Stable entries. Url= and GPGKey=, the trust
        # root, stay byte-for-byte what the hash covered.
        installed = self.destination.read_text().splitlines()
        pinned = DESCRIPTOR.read_text().splitlines()
        self.assertEqual(installed[0], "[Flatpak Repo]")
        self.assertEqual(installed[1], "DeployCollectionID=org.flathub.Stable")
        self.assertEqual([installed[0]] + installed[2:], pinned)
        self.assertEqual(self.destination.stat().st_mode & 0o777, 0o644)
        self.assertFalse((self.root / "tmp/flathub.flatpakrepo").exists())

    def test_sudo_rs_takes_usr_bin_sudo_and_su_rs_loses_setuid(self):
        # #1229: /usr/bin/sudo becomes sudo-rs itself (a hard link, so it
        # stays a setuid regular file), C sudo's sudoedit link follows it, and
        # the unused su-rs is not left setuid root.
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        bindir = self.root / "usr/bin"
        self.assertTrue((bindir / "sudo").samefile(bindir / "sudo-rs"))
        self.assertFalse((bindir / "sudo").is_symlink())
        self.assertEqual(os.readlink(bindir / "sudoedit"), "sudo")
        self.assertTrue((bindir / "sudo-rs").stat().st_mode & 0o4000)
        self.assertFalse((bindir / "su-rs").stat().st_mode & 0o4000)

    def test_sudo_rs_without_setuid_fails_and_leaves_c_sudo(self):
        (self.root / "usr/bin/sudo-rs").chmod(0o755)
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.root / "usr/bin/sudo").read_text(), "C sudo\n")

    def test_sudo_rs_not_labelled_sudo_exec_t_fails(self):
        # The policy's sudo_exec_t label is what makes the domain transition
        # match C sudo's.
        result = self.run_script(SUDO_LABEL="system_u:object_r:bin_t:s0")
        self.assertNotEqual(result.returncode, 0)

    def test_cp_linked_to_uutils_fails(self):
        # #1431: bootupd copies GRUB onto the vfat ESP with `cp -a`; uutils cp
        # fails there with EOPNOTSUPP, so no bootc install of the image works.
        cp = self.root / "usr/bin/cp"
        cp.unlink()
        cp.symlink_to("uutils-coreutils")
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("/usr/bin/cp is uutils", result.stderr)

    def test_firewall_defaults_to_gnome_os_lax_zone_through_the_symlink(self):
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        conf = self.root / "etc/firewalld/firewalld.conf"
        # firewalld owns the variant symlink; replacing it with a plain file
        # would stop package updates from reaching the config.
        self.assertTrue(conf.is_symlink())
        self.assertEqual(
            conf.read_text().splitlines(),
            ["DefaultZone=lax", "IPv6_rpfilter=loose", "FirewallBackend=nftables"],
        )

    def test_changed_descriptor_is_rejected_without_replacing_existing_remote(self):
        self.input.write_bytes(self.input.read_bytes() + b"Url=https://example.invalid/attacker\n")
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.destination.read_bytes(), self.original)


if __name__ == "__main__":
    unittest.main()
