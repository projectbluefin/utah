"""An interactive install must leave the user a way to create an account.

Bluefin and Dakota create the first account in GNOME Initial Setup, so their
installer recipes skip the user step. Utah does not ship gnome-initial-setup
(Hummingbird and the factory do not build it), so a recipe without the user
step installed a system that booted to GDM with no account to log in to. The
LUKS e2e never noticed: it hands fisherman a recipe with a "user" directly.
"""
import json
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RECIPE = ROOT / "iso/live/src/etc/bootc-installer/recipe.json"
IMAGES = ROOT / "iso/live/src/etc/bootc-installer/images.json"
CONTRACT = ROOT / "contracts/bluefin-desktop.toml"


def ships_initial_setup():
    return "gnome-initial-setup" in CONTRACT.read_text()


class InstallerAccountTests(unittest.TestCase):
    def test_recipe_parses(self):
        json.loads(RECIPE.read_text())
        tomllib.loads(CONTRACT.read_text())

    def test_account_is_created_somewhere(self):
        steps = json.loads(RECIPE.read_text())["steps"]
        templates = {step["template"] for step in steps.values()}
        self.assertTrue(
            "user" in templates or ships_initial_setup(),
            "the installer recipe has no user step and the image does not ship "
            "gnome-initial-setup: an interactive install would have no account",
        )

    def test_images_json_agrees_with_the_recipe(self):
        steps = json.loads(RECIPE.read_text())["steps"]
        has_user_step = any(s["template"] == "user" for s in steps.values())
        image = json.loads(IMAGES.read_text())["images"][0]
        if has_user_step:
            # bootc-installer hides the user step unless the image asks for it.
            self.assertTrue(image.get("needs_user_creation", True))


if __name__ == "__main__":
    unittest.main()
