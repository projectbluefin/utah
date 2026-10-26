"""Exercise the image-info build patch without composing an OS image."""
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
LEGACY = '''#!/bin/bash
IMAGE_TAG=latest
# .status.booted.image.image.image is Common's bootc JSON selector.
BOOTED_REF="${TEST_BOOTED_REF:-}"
case "${BOOTED_REF}" in
    *@*) ;;
    *:*) IMAGE_TAG="${BOOTED_REF##*:}" ;;
esac
echo -n "utah:${IMAGE_TAG}"
if [[ "$(rpm-ostree status --booted)" =~ "signed" ]]; then
    echo -n " 🔐"
else
    echo -n -e " \\033[5m🔓\\033[0m"
fi
'''


class ImageInfoPatchTests(unittest.TestCase):
    def patch(self, path):
        source = (ROOT / 'scripts/configure-services.sh').read_text()
        block = source.split('IMAGE_INFO_SH=/usr/bin/ublue-image-info.sh\n', 1)[1]
        block = block.split('# Non-interactive login shells', 1)[0]
        # Isolate the boot snapshot from the host running these tests.
        block = block.replace('/run/ublue-os/booted-image', str(path.parent / 'snapshot'))
        return subprocess.run(['bash', '-eu', '-c', 'IMAGE_INFO_SH="$1"\n' + block,
                               'patch', str(path)], capture_output=True, text=True)

    def test_legacy_without_rpm_ostree_and_repeat_patch(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'image-info.sh'
            path.write_text(LEGACY)
            for _ in range(2):
                result = self.patch(path)
                self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn('rpm-ostree', path.read_text())
            for ref, tag in [('', 'latest'), ('ghcr.io/projectbluefin/utah:testing', 'testing'),
                             ('ghcr.io/projectbluefin/utah@sha256:abc', 'latest')]:
                result = subprocess.run(['bash', str(path)], env={'TEST_BOOTED_REF': ref},
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, 'utah:' + tag)
                self.assertEqual(result.stderr, '')
            (path.parent / 'snapshot').write_text('ghcr.io/projectbluefin/utah:stable\n')
            result = subprocess.run(['bash', str(path)], capture_output=True, text=True)
            self.assertEqual(result.stdout, 'utah:stable')
            self.assertEqual(result.stderr, '')

    def test_unknown_rpm_ostree_probe_fails_build(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'image-info.sh'
            path.write_text(LEGACY.replace('rpm-ostree status --booted', 'rpm-ostree status --json'))
            result = self.patch(path)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('re-fit the rpm-ostree removal', result.stderr)
