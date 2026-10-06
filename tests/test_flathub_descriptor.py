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
        uupd = self.root / "tmp/uupd"
        uupd.mkdir(parents=True)
        (uupd / "uupd").write_text("#!/bin/sh\nexit 0\n")
        (uupd / "uupd.service").write_text("[Service]\nExecStart=/usr/bin/uupd\n")
        (uupd / "uupd.timer").write_text("[Timer]\nOnBootSec=1h\n")
        # configure-services.sh generates brewfile.preinstall from the
        # Brewfile through the installed desktop-contract parser.
        brewfile = self.root / "usr/share/ublue-os/homebrew/system-flatpaks.Brewfile"
        brewfile.parent.mkdir(parents=True)
        brewfile.write_text('flatpak "org.mozilla.firefox"\n')
        parser = self.root / "usr/local/libexec/utah-verify-desktop-contract"
        parser.parent.mkdir(parents=True)
        parser.write_bytes((ROOT / "scripts/verify-desktop-contract.py").read_bytes())
        parser.chmod(0o755)

    def run_script(self):
        source = (ROOT / "scripts/configure-services.sh").read_text()
        # Redirect image paths once; preserve real hashing/install/remove commands.
        source = re.sub(r"/(?:usr|etc|tmp)(?=/)",
                        lambda match: f"{self.root}{match.group()}", source)
        transport_and_services = '''\
systemctl() { return 0; }
authselect() { return 0; }
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
            env={**os.environ, "ENABLE_SSHD": "0", "DESCRIPTOR_INPUT": str(self.input)},
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

    def test_generated_flathub_entries_pin_the_flathub_collection(self):
        # tuna-os is a GPG-less OCI remote configured beside Flathub; an entry
        # without CollectionID would resolve from it too.
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        generated = (self.root / "usr/share/flatpak/preinstall.d/brewfile.preinstall").read_text()
        self.assertEqual(
            generated,
            "[Flatpak Preinstall org.mozilla.firefox]\nBranch=stable\n"
            "IsRuntime=false\nCollectionID=org.flathub.Stable\n\n",
        )

    def test_changed_descriptor_is_rejected_without_replacing_existing_remote(self):
        self.input.write_bytes(self.input.read_bytes() + b"Url=https://example.invalid/attacker\n")
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.destination.read_bytes(), self.original)


if __name__ == "__main__":
    unittest.main()
