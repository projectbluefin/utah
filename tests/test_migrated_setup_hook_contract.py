"""Execute all four migrated hooks against both libsetup version contracts.

Scratch roots isolate Firefox copies, DMI probes and Homebrew state. A failed
body must leave no stamp with the new API; deliberate skips commit, transient
skips retry, and the legacy compatibility path still succeeds and runs once.
"""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOOK_ROOT = ROOT / "system_files/shared/usr/share/ublue-os"
HOOKS = {
    "tailscale": ("privileged-setup.hooks.d/10-tailscale.sh", "tailscale"),
    "ucsi": ("privileged-setup.hooks.d/11-framework-ucsi-workaround.sh", "framework-ucsi-workaround"),
    "flatpaks": ("privileged-setup.hooks.d/99-flatpaks.sh", "flatpaks"),
    "framework": ("user-setup.hooks.d/20-framework.sh", "20-framework"),
}
BASH = shutil.which("bash")
LIBSETUP = '''\
version-script-check() {
    local recorded=""
    [[ ! -f "$STAMP" ]] || read -r recorded < "$STAMP" || true
    [[ "$recorded" != "$1|$3" ]]
}
version-script-commit() { printf '%s|%s\\n' "$1" "$3" > "$STAMP"; }
version-script() {
    version-script-check "$@" || return 1
    version-script-commit "$@"
}
'''
TOOLS = {
    "tailscale": '''#!/usr/bin/bash
printf '%s\\n' "$*" >> "$ACTIONS"
exit "${BODY_EXIT:-0}"
''',
    "rpm-ostree": '''#!/usr/bin/bash
if [[ $# = 1 ]]; then
    printf '%s\\n' "${KARGS:-}"
else
    printf '%s\\n' "$*" >> "$ACTIONS"
    exit "${BODY_EXIT:-0}"
fi
''',
    "brew": '''#!/usr/bin/bash
if [[ "$1" = list ]]; then
    [[ -f "$BREW_INSTALLED" ]]
else
    printf '%s\\n' "$*" >> "$ACTIONS"
    [[ "${BODY_EXIT:-0}" = 0 ]] || exit "$BODY_EXIT"
    printf 'installed\\n' > "$BREW_INSTALLED"
fi
''',
}


class HookEnvironment:
    def __init__(self, case, kind, legacy=False):
        temporary = tempfile.TemporaryDirectory(prefix="migrated-hook-")
        case.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.kind = kind
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.stamp = self.root / "stamp"
        self.actions = self.root / "actions"
        self.vendor = self.root / "dmi/vendor"
        self.product = self.root / "dmi/product"
        self.vendor.parent.mkdir()
        self.vendor.write_text("Framework\n")
        self.product.write_text("Laptop Intel Core Ultra\n")
        self.brew_prefix = self.root / "brew"
        self.brew_prefix.mkdir()
        self.firefox = self.root / "firefox"
        self.firefox.mkdir()
        (self.firefox / "bluefin.js").write_text("pref('x', 1);\n")
        self.preferences = self.root / "flatpak/extension/org.mozilla.firefox.systemconfig/x86_64/stable/defaults/pref"
        self.env = {
            "PATH": str(self.bin), "HOME": str(self.root), "PKEXEC_UID": "1000",
            "STAMP": str(self.stamp), "ACTIONS": str(self.actions),
            "TEST_LIBSETUP": str(self.root / "libsetup.sh"),
            "TEST_DMI": str(self.vendor.parent),
            "TEST_BREW_PREFIX": str(self.brew_prefix),
            "TEST_FIREFOX": str(self.firefox),
            "TEST_FLATPAK": str(self.root / "flatpak"),
            "BREW_INSTALLED": str(self.root / "brew-installed"),
        }
        library = LIBSETUP
        if legacy:
            library = library.replace("version-script-check", "legacy-check")
            library = library.replace("version-script-commit", "legacy-commit")
        (self.root / "libsetup.sh").write_text(library)
        for tool in ("mkdir", "rm", "cat", "grep", "cut"):
            (self.bin / tool).symlink_to(shutil.which(tool))
        self.install("arch", "#!/usr/bin/bash\nprintf 'x86_64\\n'\n")
        self.install("getent", "#!/usr/bin/bash\nprintf 'alice:x:1000:1000::/home/alice:/bin/bash\\n'\n")
        for tool, body in TOOLS.items():
            self.install(tool, body)

    def install(self, name, body):
        path = self.bin / name
        path.write_text(body)
        path.chmod(0o755)

    def run(self):
        path, _ = HOOKS[self.kind]
        source = (HOOK_ROOT / path).read_text().replace(
            "source /usr/lib/ublue/setup-services/libsetup.sh", 'source "$TEST_LIBSETUP"',
        )
        replacements = {
            "/sys/devices/virtual/dmi/id/chassis_vendor": "${TEST_DMI}/vendor",
            "/sys/devices/virtual/dmi/id/product_name": "${TEST_DMI}/product",
            "/home/linuxbrew/.linuxbrew": "${TEST_BREW_PREFIX}",
            "/usr/share/ublue-os/firefox-config": "${TEST_FIREFOX}",
            "/var/lib/flatpak": "${TEST_FLATPAK}",
        }
        for original, scratch in replacements.items():
            source = source.replace(original, scratch)
        wrapper = self.root / "hook.sh"
        wrapper.write_text(source)
        return subprocess.run([BASH, str(wrapper)], env=self.env,
                              capture_output=True, text=True)

    def assert_stamped(self, case):
        version = 2 if self.kind == "flatpaks" else 1
        case.assertEqual(self.stamp.read_text().strip(), f"{HOOKS[self.kind][1]}|{version}")

    def assert_effect(self, case):
        expected = {
            "tailscale": "set --operator=alice\n",
            "ucsi": "kargs --append-if-missing=usbcore.autosuspend=-1\n",
            "framework": "install fw-ectool\n",
        }
        if self.kind == "flatpaks":
            case.assertEqual((self.preferences / "bluefin.js").read_text(), "pref('x', 1);\n")
        else:
            case.assertEqual(self.actions.read_text(), expected[self.kind])


class MigratedSetupHookContractTests(unittest.TestCase):
    def test_all_four_hooks_succeed_and_skip_after_commit_on_both_libraries(self):
        for kind in HOOKS:
            for legacy in (False, True):
                with self.subTest(hook=kind, legacy=legacy):
                    env = HookEnvironment(self, kind, legacy=legacy)
                    first = env.run()
                    self.assertEqual(first.returncode, 0, first.stderr)
                    env.assert_stamped(self)
                    env.assert_effect(self)
                    if kind == "flatpaks":
                        (env.preferences / "bluefin.js").write_text("user managed\n")
                    second = env.run()
                    self.assertEqual(second.returncode, 0, second.stderr)
                    if kind == "flatpaks":
                        self.assertEqual((env.preferences / "bluefin.js").read_text(), "user managed\n")
                    else:
                        env.assert_effect(self)

    def test_all_four_body_failures_retry_before_committing(self):
        for kind in HOOKS:
            with self.subTest(hook=kind):
                env = HookEnvironment(self, kind)
                if kind == "flatpaks":
                    collision = env.preferences / "bluefin.js"
                    collision.mkdir(parents=True)
                else:
                    env.env["BODY_EXIT"] = "1"
                failed = env.run()
                self.assertNotEqual(failed.returncode, 0, failed.stderr)
                self.assertFalse(env.stamp.exists())
                if kind == "flatpaks":
                    collision.rmdir()
                else:
                    env.env["BODY_EXIT"] = "0"
                    env.actions.unlink()
                recovered = env.run()
                self.assertEqual(recovered.returncode, 0, recovered.stderr)
                env.assert_stamped(self)
                env.assert_effect(self)

    def test_flatpaks_version_one_stamp_reruns_stale_preference_cleanup(self):
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                env = HookEnvironment(self, "flatpaks", legacy=legacy)
                env.stamp.write_text("flatpaks|1\n")
                env.preferences.mkdir(parents=True)
                stale = env.preferences / "old-bluefin-default.js"
                stale.write_text("retired preference\n")
                custom = env.preferences / "user-custom.js"
                custom.write_text("user preference\n")
                result = env.run()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse(stale.exists())
                self.assertEqual(custom.read_text(), "user preference\n")
                env.assert_effect(self)
                env.assert_stamped(self)

    def test_framework_deliberate_skips_commit_without_running_a_body(self):
        for kind, reason in (("ucsi", "vendor"), ("ucsi", "product"),
                             ("ucsi", "already-applied"), ("framework", "vendor")):
            with self.subTest(hook=kind, reason=reason):
                env = HookEnvironment(self, kind)
                if reason == "vendor":
                    env.vendor.write_text("Other vendor\n")
                elif reason == "product":
                    env.product.write_text("AMD Ryzen\n")
                else:
                    env.env["KARGS"] = "quiet usbcore.autosuspend=-1"
                result = env.run()
                self.assertEqual(result.returncode, 0, result.stderr)
                env.assert_stamped(self)
                self.assertFalse(env.actions.exists())

    def test_framework_transient_skips_retry_when_prerequisite_appears(self):
        for kind, reason in (("ucsi", "dmi"), ("ucsi", "rpm-ostree"),
                             ("framework", "dmi"), ("framework", "brew"),
                             ("framework", "prefix")):
            for legacy in (False, True):
                with self.subTest(hook=kind, reason=reason, legacy=legacy):
                    env = HookEnvironment(self, kind, legacy=legacy)
                    if reason == "dmi":
                        env.vendor.unlink()
                    elif reason == "prefix":
                        env.brew_prefix.rmdir()
                    else:
                        (env.bin / reason).unlink()
                    deferred = env.run()
                    self.assertEqual(deferred.returncode, 0, deferred.stderr)
                    self.assertFalse(env.stamp.exists())
                    self.assertFalse(env.actions.exists())
                    if reason == "dmi":
                        env.vendor.write_text("Framework\n")
                    elif reason == "prefix":
                        env.brew_prefix.mkdir()
                    else:
                        env.install(reason, TOOLS[reason])
                    recovered = env.run()
                    self.assertEqual(recovered.returncode, 0, recovered.stderr)
                    env.assert_stamped(self)
                    env.assert_effect(self)

    def test_tailscale_missing_binary_defers_and_recovers(self):
        env = HookEnvironment(self, "tailscale")
        (env.bin / "tailscale").unlink()
        deferred = env.run()
        self.assertEqual(deferred.returncode, 0, deferred.stderr)
        self.assertFalse(env.stamp.exists())
        env.install("tailscale", TOOLS["tailscale"])
        recovered = env.run()
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        env.assert_stamped(self)
        env.assert_effect(self)

    def test_tailscale_root_or_invalid_caller_never_gets_operator(self):
        for uid in (None, "", "0", "00", "root"):
            with self.subTest(uid=uid):
                env = HookEnvironment(self, "tailscale")
                if uid is None:
                    del env.env["PKEXEC_UID"]
                else:
                    env.env["PKEXEC_UID"] = uid
                result = env.run()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse(env.stamp.exists())
                self.assertFalse(env.actions.exists())

    def test_flatpaks_missing_firefox_config_skips_cleanly(self):
        env = HookEnvironment(self, "flatpaks")
        (env.firefox / "bluefin.js").unlink()
        result = env.run()
        self.assertEqual(result.returncode, 0, result.stderr)
        env.assert_stamped(self)
        self.assertEqual(list(env.preferences.iterdir()), [])


class FirstBootEnablementTests(unittest.TestCase):
    def test_contract_and_preset_enable_stats_refresh_timer(self):
        contract = (ROOT / "contracts/bluefin-desktop.toml").read_text()
        preset = (ROOT / "system_files/shared/usr/lib/systemd/system-preset/85-utah-desktop.preset").read_text()
        services = (ROOT / "scripts/configure-services.sh").read_text()
        self.assertIn('"bluefin-stats-refresh.timer"', contract)
        self.assertIn("enable bluefin-stats-refresh.timer", preset)
        self.assertIn("enable_unit bluefin-stats-refresh.timer", services)


if __name__ == "__main__":
    unittest.main()
