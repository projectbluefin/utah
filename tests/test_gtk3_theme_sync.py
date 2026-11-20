"""GTK3 apps follow the light/dark style (#1342).

GTK3 reads org.gnome.desktop.interface gtk-theme, never color-scheme, so the
image defaults gtk-theme to adw-gtk3 and ublue-gtk3-theme-sync switches it to
adw-gtk3-dark and back, leaving a theme the user picked alone.
"""

import os
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHARED = ROOT / "system_files/shared"
SCRIPT = SHARED / "usr/libexec/ublue-gtk3-theme-sync"
UNIT = SHARED / "usr/lib/systemd/user/ublue-gtk3-theme-sync.service"

# A gsettings stand-in: one file per key holding the printed GVariant, and a
# monitor that reports nothing and exits, which ends the script's watch loop
# after its login-time sync.
FAKE_GSETTINGS = """#!/usr/bin/bash
store="${GSETTINGS_STORE:?}"
case "$1" in
get) cat "$store/$3" ;;
set) printf "'%s'\\n" "$4" > "$store/$3" ;;
monitor) exit 0 ;;
*) exit 2 ;;
esac
"""


class ThemeSyncScriptTests(unittest.TestCase):
    def run_sync(self, scheme, theme):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bin_dir = tmp / "bin"
            store = tmp / "store"
            bin_dir.mkdir()
            store.mkdir()
            fake = bin_dir / "gsettings"
            fake.write_text(FAKE_GSETTINGS)
            fake.chmod(0o755)
            (store / "color-scheme").write_text(f"'{scheme}'\n")
            (store / "gtk-theme").write_text(f"'{theme}'\n")
            env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", GSETTINGS_STORE=str(store))
            result = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True, check=False)
            # The monitor ending is a failure systemd restarts (Restart=on-failure).
            self.assertEqual(result.returncode, 1, result.stderr)
            return (store / "gtk-theme").read_text().strip()

    def test_dark_style_selects_the_dark_variant(self):
        self.assertEqual(self.run_sync("prefer-dark", "adw-gtk3"), "'adw-gtk3-dark'")

    def test_light_and_default_select_the_light_variant(self):
        for scheme in ("default", "prefer-light"):
            with self.subTest(scheme=scheme):
                self.assertEqual(self.run_sync(scheme, "adw-gtk3-dark"), "'adw-gtk3'")

    def test_a_theme_picked_by_hand_is_left_alone(self):
        for theme in ("Adwaita", "HighContrast"):
            with self.subTest(theme=theme):
                self.assertEqual(self.run_sync("prefer-dark", theme), f"'{theme}'")


class ThemeSyncWiringTests(unittest.TestCase):
    def test_default_theme_is_adw_gtk3_and_the_contract_pins_it(self):
        override = (SHARED / "usr/share/glib-2.0/schemas/zz3-bluefin-unsupported-stuff.gschema.override").read_text()
        section = override.split("[org.gnome.desktop.interface]", 1)[1]
        self.assertIn("gtk-theme='adw-gtk3'", section.split("\n[", 1)[0])
        contract = tomllib.loads((ROOT / "contracts/bluefin-desktop.toml").read_text())
        self.assertEqual(contract["gsettings"]["values"]["org.gnome.desktop.interface gtk-theme"], "'adw-gtk3'")
        self.assertIn("org.gtk.Gtk3theme.adw-gtk3-dark", contract["flatpak"]["apps"])

    def test_service_is_enabled_for_every_user(self):
        contract = tomllib.loads((ROOT / "contracts/bluefin-desktop.toml").read_text())
        self.assertIn("ublue-gtk3-theme-sync.service", contract["services"]["user_enabled"])
        preset = (SHARED / "usr/lib/systemd/user-preset/85-utah-desktop.preset").read_text()
        self.assertIn("enable ublue-gtk3-theme-sync.service\n", preset)
        services = (ROOT / "scripts/configure-services.sh").read_text()
        self.assertIn("systemctl --global enable ublue-gtk3-theme-sync.service\n", services)

    def test_unit_runs_the_script_in_graphical_sessions_only(self):
        unit = UNIT.read_text()
        self.assertIn("ExecStart=/usr/libexec/ublue-gtk3-theme-sync\n", unit)
        self.assertIn("WantedBy=graphical-session.target\n", unit)
        self.assertIn("ConditionEnvironment=!XDG_SESSION_CLASS=greeter\n", unit)
        self.assertTrue(os.access(SCRIPT, os.X_OK))


if __name__ == "__main__":
    unittest.main()
