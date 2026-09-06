"""Unit coverage for scripts/flavors.py.

flavors.py is the single source for four things that must agree: the build
matrix, the promote matrix, the release matrix, and whether the kernel cache
image is built at all. `just check` only runs `flavors.py list >/dev/null`,
which proves the file parses and nothing about what it answers -- a wrong
`needs-kernel` wastes a 45-minute kernel compile, and a wrong `list-kernel`
submits flavors against a base image that was never built.

Each query is exercised against synthetic config trees so the assertions state
the mapping rather than restate today's config/flavors.json, plus a pass over
the real config so the shipped file stays inside the contract.
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "flavors.py"
REAL_CONFIG = REPO / "config" / "flavors.json"

ALL_FLAVORS = ["main", "nvidia", "gaming", "nvidia-gaming"]


def run(script_path, *args):
    return subprocess.run(
        [sys.executable, str(script_path), *args],
        capture_output=True,
        text=True,
    )


@pytest.fixture
def flavors_cli(tmp_path):
    """Build a throwaway repo layout so config/flavors.json can be varied.

    CONFIG is resolved from the script's own location, so the script has to be
    copied next to a synthetic config rather than pointed at one.
    """

    def build(config):
        root = tmp_path / f"tree{len(list(tmp_path.iterdir()))}"
        (root / "scripts").mkdir(parents=True)
        (root / "config").mkdir()
        shutil.copy(SCRIPT, root / "scripts" / "flavors.py")
        payload = config if isinstance(config, str) else json.dumps(config)
        (root / "config" / "flavors.json").write_text(payload)
        return root / "scripts" / "flavors.py"

    return build


def test_list_returns_configured_flavors_in_order(flavors_cli):
    cli = flavors_cli({"flavors": ["main", "nvidia"], "retired": {}})
    result = run(cli, "list")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == ["main", "nvidia"]


def test_list_is_the_default_query(flavors_cli):
    cli = flavors_cli({"flavors": ALL_FLAVORS, "retired": {}})
    assert json.loads(run(cli).stdout) == ALL_FLAVORS


def test_images_maps_every_flavor_to_its_image_name(flavors_cli):
    cli = flavors_cli({"flavors": ALL_FLAVORS, "retired": {}})
    assert json.loads(run(cli, "images").stdout) == [
        {"image": "utah"},
        {"image": "utah-nvidia"},
        {"image": "utah-gaming"},
        {"image": "utah-nvidia-gaming"},
    ]


def test_releases_promotes_testing_to_stable_for_every_flavor(flavors_cli):
    cli = flavors_cli({"flavors": ["main", "gaming"], "retired": {}})
    assert json.loads(run(cli, "releases").stdout) == [
        {"image": "utah", "source_tag": "testing", "target_tag": "stable"},
        {"image": "utah-gaming", "source_tag": "testing", "target_tag": "stable"},
    ]


@pytest.mark.parametrize(
    ("flavors", "expected"),
    [
        (["main"], "false"),
        ([], "false"),
        (["main", "nvidia"], "true"),
        (["gaming"], "true"),
        (["nvidia-gaming"], "true"),
    ],
)
def test_needs_kernel_is_true_only_when_a_non_main_flavor_is_built(
    flavors_cli, flavors, expected
):
    cli = flavors_cli({"flavors": flavors, "retired": {}})
    assert run(cli, "needs-kernel").stdout.strip() == expected


def test_list_main_and_list_kernel_partition_the_flavor_set(flavors_cli):
    cli = flavors_cli({"flavors": ALL_FLAVORS, "retired": {}})
    main = json.loads(run(cli, "list-main").stdout)
    kernel = json.loads(run(cli, "list-kernel").stdout)
    assert main == ["main"]
    assert kernel == ["nvidia", "gaming", "nvidia-gaming"]
    assert main + kernel == ALL_FLAVORS
    assert set(main) & set(kernel) == set()


def test_list_main_is_empty_when_main_is_retired(flavors_cli):
    cli = flavors_cli({"flavors": ["nvidia"], "retired": {"main": "off"}})
    assert json.loads(run(cli, "list-main").stdout) == []
    assert json.loads(run(cli, "list-kernel").stdout) == ["nvidia"]


def test_unknown_flavor_in_config_fails_every_query(flavors_cli):
    cli = flavors_cli({"flavors": ["main", "utah-nvidia"], "retired": {}})
    for query in ("list", "images", "releases", "needs-kernel", "list-kernel"):
        result = run(cli, query)
        assert result.returncode != 0, f"{query} accepted an unknown flavor"
        assert "unknown flavor" in result.stderr
        assert "utah-nvidia" in result.stderr


def test_unknown_query_fails_rather_than_printing_nothing(flavors_cli):
    cli = flavors_cli({"flavors": ALL_FLAVORS, "retired": {}})
    result = run(cli, "list-gaming")
    assert result.returncode != 0
    assert "unknown query: list-gaming" in result.stderr
    assert result.stdout.strip() == ""


def test_malformed_config_fails_loudly(flavors_cli):
    cli = flavors_cli("{not json")
    result = run(cli, "list")
    assert result.returncode != 0
    assert result.stdout.strip() == ""


def test_shipped_config_only_names_known_flavors():
    config = json.loads(REAL_CONFIG.read_text())
    assert set(config["flavors"]) <= set(ALL_FLAVORS)
    assert len(config["flavors"]) == len(set(config["flavors"]))


def test_shipped_config_agrees_across_all_queries():
    """The four consumers must describe the same set, from the real config."""
    flavors = json.loads(run(SCRIPT, "list").stdout)
    images = json.loads(run(SCRIPT, "images").stdout)
    releases = json.loads(run(SCRIPT, "releases").stdout)
    main = json.loads(run(SCRIPT, "list-main").stdout)
    kernel = json.loads(run(SCRIPT, "list-kernel").stdout)

    assert len(images) == len(flavors)
    assert [r["image"] for r in releases] == [i["image"] for i in images]
    assert main + kernel == flavors
    assert run(SCRIPT, "needs-kernel").stdout.strip() == (
        "true" if kernel else "false"
    )
