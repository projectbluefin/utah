"""Exercise the image-info build step without composing an OS image."""
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
# Shape of Common's ublue-image-info.sh: it asks the booted-image reader for the
# tag. The rpm-ostree lock glyph is the legacy block Utah still strips.
IMAGE_INFO = '''#!/bin/bash
IMAGE_TAG=latest
BOOTED_TAG="$("@READER@" tag 2>/dev/null)"
if [[ -n "${BOOTED_TAG}" ]]; then IMAGE_TAG="${BOOTED_TAG}"; fi
echo -n "utah:${IMAGE_TAG}"
if [[ "$(rpm-ostree status --booted)" =~ "signed" ]]; then
    echo -n " 🔐"
else
    echo -n -e " \\033[5m🔓\\033[0m"
fi
'''
# Stand-in for Common's /usr/libexec/ublue-booted-image: prints the tag of the
# ref in the snapshot file, nothing when there is none.
READER = '''#!/bin/bash
# Falls back to the boot-time snapshot at /run/ublue-os/booted-image.
ref="$(cat "@SNAPSHOT@" 2>/dev/null)" || exit 1
case "${ref}" in *@*) ;; *:*) echo "${ref##*:}" ;; esac
'''


class ImageInfoPatchTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.info = self.dir / 'image-info.sh'
        self.reader = self.dir / 'ublue-booted-image'
        self.snapshot = self.dir / 'snapshot'
        self.info.write_text(IMAGE_INFO.replace('@READER@', str(self.reader)))
        self.reader.write_text(READER.replace('@SNAPSHOT@', str(self.snapshot)))
        self.reader.chmod(0o755)

    def patch(self):
        source = (ROOT / 'scripts/configure-services.sh').read_text()
        block = source.split('IMAGE_INFO_SH=/usr/bin/ublue-image-info.sh\n', 1)[1]
        block = block.split('# Non-interactive login shells', 1)[0]
        # Point the reader and snapshot at the sandbox, not the test host.
        block = block.replace('BOOTED_IMAGE_BIN=/usr/libexec/ublue-booted-image', 'BOOTED_IMAGE_BIN="$2"')
        block = block.replace('/run/ublue-os/booted-image', str(self.snapshot))
        return subprocess.run(['bash', '-eu', '-c', 'IMAGE_INFO_SH="$1"\n' + block,
                               'patch', str(self.info), str(self.reader)],
                              capture_output=True, text=True)

    def run_info(self):
        return subprocess.run(['bash', str(self.info)], capture_output=True, text=True)

    def test_rpm_ostree_removed_repeat_safe_and_snapshot_read(self):
        for _ in range(2):
            result = self.patch()
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('rpm-ostree', self.info.read_text())
        result = self.run_info()
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, 'utah:latest', ''))
        self.snapshot.write_text('ghcr.io/projectbluefin/utah:stable\n')
        result = self.run_info()
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, 'utah:stable', ''))
        self.snapshot.write_text('ghcr.io/projectbluefin/utah@sha256:abc\n')
        self.assertEqual(self.run_info().stdout, 'utah:latest')

    def test_unknown_rpm_ostree_probe_fails_build(self):
        self.info.write_text(self.info.read_text().replace('rpm-ostree status --booted', 'rpm-ostree status --json'))
        result = self.patch()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('re-fit the rpm-ostree removal', result.stderr)

    def test_image_info_not_using_reader_fails_build(self):
        self.info.write_text(self.info.read_text().replace(str(self.reader), '/bin/true'))
        result = self.patch()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('re-check the booted-image snapshot fallback', result.stderr)

    def test_reader_without_snapshot_fallback_fails_build(self):
        self.reader.write_text('#!/bin/bash\nexit 1\n')
        result = self.patch()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('re-check the booted-image snapshot fallback', result.stderr)
