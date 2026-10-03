"""Run first-boot hooks in a disposable filesystem, never against host /var.

Only libsetup and account/Tailscale lookups are simulated. Firefox preferences
are copied by the real /usr/bin/cp into a scratch Flatpak tree. unittest's normal
discovery collects these cases; they are not a repeat-boot VM proof.
"""

import importlib.util
import shutil
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOOKS_DIR = ROOT / "system_files/shared/usr/share/ublue-os"
TAILSCALE_HOOK = HOOKS_DIR / "privileged-setup.hooks.d/10-tailscale.sh"
FLATPAKS_HOOK = HOOKS_DIR / "privileged-setup.hooks.d/99-flatpaks.sh"
PRESET = ROOT / "system_files/shared/usr/lib/systemd/system-preset/85-utah-desktop.preset"
CONTRACT = ROOT / "contracts/bluefin-desktop.toml"
BASH = shutil.which("bash")

# The read-only check and separate commit model common's new API. The legacy
# wrapper intentionally stamps on check, so compatibility tests do not promise
# retry-after-failure on a common image that still has only that helper.
STUB_LIBSETUP = '''\
version-script-check() {
    local recorded=""
    if [[ -f "$SETUP_CHECKER_FILE" ]]; then
        read -r recorded < "$SETUP_CHECKER_FILE" || true
    fi
    [[ "$recorded" != "$1|$3" ]]
}
version-script-commit() {
    printf '%s|%s\\n' "$1" "$3" > "$SETUP_CHECKER_FILE"
}
version-script() {
    version-script-check "$@" || return 1
    version-script-commit "$@"
}
'''

STUB_GETENT = '''#!/usr/bin/bash
case "$2" in
    1000) printf 'alice:x:1000:1000::/home/alice:/bin/bash\\n' ;;
    0) printf 'root:x:0:0::/root:/bin/bash\\n' ;;
    1001) printf 'root:x:0:0::/root:/bin/bash\\n' ;;
    *) exit 2 ;;
esac
'''
STUB_TAILSCALE = '''#!/usr/bin/bash
printf '%s\\n' "$*" >> "$TAILSCALE_LOG"
exit "${TAILSCALE_EXIT:-0}"
'''


class FirstBootEnv:
    def __init__(self, case, legacy=False, directory=None):
        temporary = tempfile.TemporaryDirectory(prefix="first-boot-", dir=directory)
        case.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.versioning = self.root / "setup_versioning.json"
        self.tailscale_log = self.root / "tailscale.log"
        self.firefox_root = self.root / "usr/share/ublue-os/firefox-config"
        self.flatpak_root = self.root / "var/lib/flatpak"
        self.preferences = (
            self.flatpak_root
            / "extension/org.mozilla.firefox.systemconfig/x86_64/stable/defaults/pref"
        )
        self.env = {
            "PATH": str(self.bin),
            "HOME": str(self.root),
            "SETUP_CHECKER_FILE": str(self.versioning),
            "TAILSCALE_LOG": str(self.tailscale_log),
            "TEST_LIBSETUP": str(self.root / "libsetup.sh"),
            "TEST_FIREFOX_ROOT": str(self.firefox_root),
            "TEST_FLATPAK_ROOT": str(self.flatpak_root),
        }
        library = STUB_LIBSETUP
        if legacy:
            # Hide the pair from the hook, retaining legacy stamp-on-check.
            library = library.replace("version-script-check", "legacy-check")
            library = library.replace("version-script-commit", "legacy-commit")
        (self.root / "libsetup.sh").write_text(library)
        for tool in ("mkdir", "rm", "cut"):
            (self.bin / tool).symlink_to(shutil.which(tool))
        self.install("arch", "#!/usr/bin/bash\nprintf 'x86_64\\n'\n")
        self.install("getent", STUB_GETENT)

    def install(self, name, body):
        target = self.bin / name
        target.write_text(body)
        target.chmod(0o755)

    def install_tailscale(self):
        self.install("tailscale", STUB_TAILSCALE)
        self.env["PKEXEC_UID"] = "1000"

    def run_hook(self, hook=TAILSCALE_HOOK):
        # Rewrite filesystem roots only, not guard logic or commands. All writes
        # including the absolute /usr/bin/cp target are below this temp root.
        source = hook.read_text().replace(
            "source /usr/lib/ublue/setup-services/libsetup.sh",
            'source "$TEST_LIBSETUP"',
        ).replace(
            "/usr/share/ublue-os/firefox-config", "${TEST_FIREFOX_ROOT}",
        ).replace("/var/lib/flatpak", "${TEST_FLATPAK_ROOT}")
        wrapper = self.root / "hook.sh"
        wrapper.write_text(source)
        return subprocess.run(
            [BASH, str(wrapper)], env=self.env, capture_output=True, text=True,
        )

    def versioning_stamped(self, name, version):
        return self.versioning.exists() and self.versioning.read_text().strip() == f"{name}|{version}"


class TestTailscaleHook(unittest.TestCase):
    def test_missing_binary_defers_without_failing(self):
        env = FirstBootEnv(self)
        result = env.run_hook()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(env.versioning_stamped("tailscale", "1"))
        self.assertFalse(env.tailscale_log.exists())
        # The transient absence must not prevent a later successful grant.
        env.install_tailscale()
        result = env.run_hook()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(env.versioning_stamped("tailscale", "1"))
        self.assertEqual(env.tailscale_log.read_text(), "set --operator=alice\n")

    def test_present_binary_grants_operator(self):
        env = FirstBootEnv(self)
        env.install_tailscale()
        result = env.run_hook()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(env.versioning_stamped("tailscale", "1"))
        self.assertEqual(env.tailscale_log.read_text(), "set --operator=alice\n")

    def test_missing_calling_uid_defers(self):
        env = FirstBootEnv(self)
        env.install_tailscale()
        del env.env["PKEXEC_UID"]
        result = env.run_hook()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(env.versioning_stamped("tailscale", "1"))
        self.assertFalse(env.tailscale_log.exists())

    def test_root_and_invalid_callers_never_get_an_operator_grant(self):
        for uid in ("0", "00", "root", "1001", "9999", ""):
            with self.subTest(uid=uid):
                env = FirstBootEnv(self)
                env.install_tailscale()
                env.env["PKEXEC_UID"] = uid
                result = env.run_hook()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse(env.versioning.exists())
                self.assertFalse(env.tailscale_log.exists())

    def test_version_stamp_is_idempotent_on_both_common_contracts(self):
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                env = FirstBootEnv(self, legacy=legacy)
                env.install_tailscale()
                first = env.run_hook()
                self.assertEqual(first.returncode, 0, first.stderr)
                self.assertTrue(env.versioning_stamped("tailscale", "1"))
                second = env.run_hook()
                self.assertEqual(second.returncode, 0, second.stderr)
                self.assertEqual(env.tailscale_log.read_text(), "set --operator=alice\n")

    def test_failed_operator_grant_retries_with_the_read_only_pair(self):
        env = FirstBootEnv(self)
        env.install_tailscale()
        env.env["TAILSCALE_EXIT"] = "1"
        failed = env.run_hook()
        self.assertEqual(failed.returncode, 1, failed.stderr)
        self.assertFalse(env.versioning.exists())
        env.env["TAILSCALE_EXIT"] = "0"
        recovered = env.run_hook()
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        self.assertTrue(env.versioning_stamped("tailscale", "1"))
        self.assertEqual(env.tailscale_log.read_text().splitlines(), [
            "set --operator=alice", "set --operator=alice",
        ])
        env.run_hook()
        self.assertEqual(len(env.tailscale_log.read_text().splitlines()), 2)


class TestFlatpaksHook(unittest.TestCase):
    def test_missing_firefox_config_skips_cleanly(self):
        env = FirstBootEnv(self)
        result = env.run_hook(FLATPAKS_HOOK)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(env.versioning_stamped("flatpaks", "1"))
        self.assertEqual(list(env.preferences.iterdir()), [])

    def test_present_firefox_config_copies(self):
        # Exercise a relocated temporary root, including spaces, rather than
        # depending on the host's /tmp layout or matching a trace prefix.
        temporary = tempfile.TemporaryDirectory(prefix="firefox fixture ")
        self.addCleanup(temporary.cleanup)
        env = FirstBootEnv(self, directory=temporary.name)
        env.firefox_root.mkdir(parents=True)
        preferences = "pref('x', 1);\n"
        (env.firefox_root / "bluefin.js").write_text(preferences)
        result = env.run_hook(FLATPAKS_HOOK)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(env.versioning_stamped("flatpaks", "1"))
        self.assertEqual((env.preferences / "bluefin.js").read_text(), preferences)
        # A second run must not overwrite a subsequently user-managed copy.
        (env.preferences / "bluefin.js").write_text("pref('x', 2);\n")
        rerun = env.run_hook(FLATPAKS_HOOK)
        self.assertEqual(rerun.returncode, 0, rerun.stderr)
        self.assertEqual((env.preferences / "bluefin.js").read_text(), "pref('x', 2);\n")

    def test_copy_failure_does_not_stamp_and_retries(self):
        env = FirstBootEnv(self)
        env.firefox_root.mkdir(parents=True)
        (env.firefox_root / "bluefin.js").write_text("pref('x', 1);\n")
        # A directory at the destination filename makes real cp reject the
        # source file, without relying on permissions or privileged I/O.
        collision = env.preferences / "bluefin.js"
        collision.mkdir(parents=True)
        failed = env.run_hook(FLATPAKS_HOOK)
        self.assertNotEqual(failed.returncode, 0, failed.stderr)
        self.assertFalse(env.versioning.exists())
        collision.rmdir()
        recovered = env.run_hook(FLATPAKS_HOOK)
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        self.assertTrue(env.versioning_stamped("flatpaks", "1"))
        self.assertEqual((env.preferences / "bluefin.js").read_text(), "pref('x', 1);\n")

def _load_desktop_verifier():
    spec = importlib.util.spec_from_file_location(
        "verify-desktop-contract", ROOT / "scripts/verify-desktop-contract.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


DESKTOP = _load_desktop_verifier()


class TestEnablementPolicy(unittest.TestCase):
    def test_contract_validates(self):
        self.assertEqual(DESKTOP.validate_contract(tomllib.loads(CONTRACT.read_text())), [])

    def test_contract_asserts_first_boot_services(self):
        enabled = tomllib.loads(CONTRACT.read_text())["services"]["enabled"]
        self.assertIn("bluefin-stats-refresh.timer", enabled)
        self.assertIn("input-remapper.service", enabled)

    def test_preset_enables_first_boot_services(self):
        preset = PRESET.read_text()
        self.assertIn("enable bluefin-stats-refresh.timer", preset)
        self.assertIn("enable input-remapper.service", preset)


if __name__ == "__main__":
    unittest.main()
