"""Black-box coverage for the two CI guard scripts run by `just check`.

`scripts/check-download-integrity.py` is the supply-chain guard: it must fail
the build when a composition recipe resolves a mutable `releases/latest` or
fetches an executable asset with no digest/signature check beside it.
`scripts/check_workflow_outputs.py` is the workflow-wiring guard: it must fail
when a job output reads `steps.<id>.outputs` for a step id that job never
defines.

Both scripts resolve their inputs relative to the process working directory, so
every test builds a synthetic repository tree in a tmp_path and runs the real
script there. Only the two final tests look at the shipped tree.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
INTEGRITY = REPO_ROOT / "scripts" / "check-download-integrity.py"
WORKFLOW_OUTPUTS = REPO_ROOT / "scripts" / "check_workflow_outputs.py"


def run(script: Path, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(script)],
        cwd=cwd,
        capture_output=True,
        text=True,
    )


def write(root: Path, relative: str, text: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


# --------------------------------------------------------------------------
# scripts/check-download-integrity.py
# --------------------------------------------------------------------------

# Every path the guard inspects. A file dropped from that list would silently
# stop being guarded, so the suite states the list rather than trusting it.
INSPECTED = [
    "Containerfile",
    "Containerfile.kernel",
    "scripts/install-nvidia.sh",
    "scripts/install-ogc-kernel.sh",
    "scripts/configure-services.sh",
    "iso/live/src/install-flatpaks.sh",
    "iso/scripts/build-iso.sh",
]


def test_empty_tree_passes_and_reports_the_recipe_count(tmp_path):
    result = run(INTEGRITY, tmp_path)

    assert result.returncode == 0, result.stderr
    assert "checked 7 build recipes" in result.stdout


@pytest.mark.parametrize("recipe", INSPECTED)
def test_mutable_latest_release_is_rejected_in_every_inspected_recipe(tmp_path, recipe):
    write(
        tmp_path,
        recipe,
        "curl -fsSL https://github.com/example/tool/releases/latest/download/tool.tar.gz\n",
    )

    result = run(INTEGRITY, tmp_path)

    assert result.returncode == 1
    assert f"{recipe}:1: resolves a mutable latest release" in result.stderr


@pytest.mark.parametrize("suffix", ["run", "tar.gz", "tgz", "rpm", "flatpak"])
def test_unverified_executable_download_is_rejected_for_every_guarded_suffix(tmp_path, suffix):
    write(
        tmp_path,
        "Containerfile",
        f"RUN curl -fsSL https://example.invalid/v1.2.3/tool.{suffix} -o /tmp/tool\n",
    )

    result = run(INTEGRITY, tmp_path)

    assert result.returncode == 1
    assert (
        "Containerfile:1: executable download without a digest or signature check"
        in result.stderr
    )


@pytest.mark.parametrize("fetcher", ["curl", "wget"])
def test_both_fetchers_are_guarded(tmp_path, fetcher):
    write(tmp_path, "Containerfile", f"RUN {fetcher} https://example.invalid/v1/tool.rpm\n")

    result = run(INTEGRITY, tmp_path)

    assert result.returncode == 1
    assert "executable download without a digest or signature check" in result.stderr


@pytest.mark.parametrize(
    "verifier",
    [
        "sha256sum --check tool.sha256",
        "sha512sum -c tool.sha512",
        "cosign verify-blob tool",
        "gpg --verify tool.sig",
    ],
)
def test_a_verifier_anywhere_in_the_same_file_clears_its_downloads(tmp_path, verifier):
    write(
        tmp_path,
        "scripts/install-nvidia.sh",
        f"curl -fsSL https://example.invalid/v1.2.3/driver.run -o /tmp/driver.run\n{verifier}\n",
    )

    result = run(INTEGRITY, tmp_path)

    assert result.returncode == 0, result.stderr


def test_verification_is_per_file_not_repository_wide(tmp_path):
    write(
        tmp_path,
        "scripts/install-nvidia.sh",
        "curl -fsSL https://example.invalid/v1/driver.run -o /tmp/driver.run\n"
        "sha256sum --check driver.sha256\n",
    )
    write(
        tmp_path,
        "Containerfile",
        "RUN curl -fsSL https://example.invalid/v1/tool.rpm -o /tmp/tool.rpm\n",
    )

    result = run(INTEGRITY, tmp_path)

    assert result.returncode == 1
    assert "Containerfile:1" in result.stderr
    assert "install-nvidia.sh" not in result.stderr


def test_commented_out_downloads_are_ignored(tmp_path):
    write(
        tmp_path,
        "Containerfile",
        "# curl https://example.invalid/releases/latest/download/tool.tar.gz\n"
        "#   curl https://example.invalid/v1/tool.rpm\n",
    )

    result = run(INTEGRITY, tmp_path)

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "descriptor",
    [
        "flatpak remote-add --if-not-exists flathub "
        "https://dl.flathub.org/repo/flathub.flatpakrepo",
        "curl -fsSL https://dl.flathub.org/repo/appstream/x86_64.tar.gz -o /tmp/appstream.tar.gz",
    ],
)
def test_flathub_descriptors_stay_allowed_without_a_digest(tmp_path, descriptor):
    write(tmp_path, "iso/live/src/install-flatpaks.sh", f"{descriptor}\n")

    result = run(INTEGRITY, tmp_path)

    assert result.returncode == 0, result.stderr


def test_non_executable_downloads_are_not_flagged(tmp_path):
    write(
        tmp_path,
        "scripts/configure-services.sh",
        "curl -fsSL https://example.invalid/v1/config.json -o /etc/config.json\n",
    )

    result = run(INTEGRITY, tmp_path)

    assert result.returncode == 0, result.stderr


def test_a_mutable_latest_is_reported_even_when_the_file_verifies_digests(tmp_path):
    write(
        tmp_path,
        "Containerfile",
        "RUN curl -fsSL https://example.invalid/releases/latest/download/tool.rpm -o /tmp/tool.rpm\n"
        "RUN sha256sum --check /tmp/tool.sha256\n",
    )

    result = run(INTEGRITY, tmp_path)

    assert result.returncode == 1
    assert "resolves a mutable latest release" in result.stderr


def test_every_problem_line_is_reported_with_its_line_number(tmp_path):
    write(
        tmp_path,
        "Containerfile",
        "FROM scratch\n"
        "RUN curl -fsSL https://example.invalid/releases/latest/download/a.tar.gz\n"
        "RUN true\n"
        "RUN wget https://example.invalid/v1/b.rpm\n",
    )

    result = run(INTEGRITY, tmp_path)

    assert result.returncode == 1
    assert "Containerfile:2: resolves a mutable latest release" in result.stderr
    assert (
        "Containerfile:4: executable download without a digest or signature check"
        in result.stderr
    )


def test_shipped_build_recipes_pass_the_integrity_guard():
    result = run(INTEGRITY, REPO_ROOT)

    assert result.returncode == 0, result.stderr


# --------------------------------------------------------------------------
# scripts/check_workflow_outputs.py
# --------------------------------------------------------------------------

WIRED_WORKFLOW = """
name: build
jobs:
  resolve:
    runs-on: ubuntu-latest
    outputs:
      flavors: ${{ steps.matrix.outputs.flavors }}
    steps:
      - id: matrix
        run: echo "flavors=[]" >> "$GITHUB_OUTPUT"
"""

DANGLING_WORKFLOW = """
name: build
jobs:
  resolve:
    runs-on: ubuntu-latest
    outputs:
      flavors: ${{ steps.matrix.outputs.flavors }}
    steps:
      - run: echo no id here
"""


def test_no_workflow_directory_still_passes(tmp_path):
    result = run(WORKFLOW_OUTPUTS, tmp_path)

    assert result.returncode == 0, result.stderr
    assert "every referenced step id exists" in result.stdout


def test_output_wired_to_a_defined_step_passes(tmp_path):
    write(tmp_path, ".github/workflows/build.yml", WIRED_WORKFLOW)

    result = run(WORKFLOW_OUTPUTS, tmp_path)

    assert result.returncode == 0, result.stderr


def test_output_reading_a_step_id_no_step_defines_fails(tmp_path):
    write(tmp_path, ".github/workflows/build.yml", DANGLING_WORKFLOW)

    result = run(WORKFLOW_OUTPUTS, tmp_path)

    assert result.returncode == 1
    assert "job resolve output flavors reads steps.matrix" in result.stderr


def test_step_ids_do_not_leak_across_jobs(tmp_path):
    write(
        tmp_path,
        ".github/workflows/build.yml",
        """
name: build
jobs:
  producer:
    runs-on: ubuntu-latest
    steps:
      - id: matrix
        run: echo hi
  consumer:
    runs-on: ubuntu-latest
    outputs:
      flavors: ${{ steps.matrix.outputs.flavors }}
    steps:
      - run: echo hi
""",
    )

    result = run(WORKFLOW_OUTPUTS, tmp_path)

    assert result.returncode == 1
    assert "job consumer output flavors reads steps.matrix" in result.stderr


def test_jobs_without_outputs_are_skipped(tmp_path):
    write(
        tmp_path,
        ".github/workflows/build.yml",
        """
name: build
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - run: echo ${{ steps.nothing.outputs.value }}
""",
    )

    result = run(WORKFLOW_OUTPUTS, tmp_path)

    assert result.returncode == 0, result.stderr


def test_every_dangling_reference_in_every_workflow_is_reported(tmp_path):
    write(tmp_path, ".github/workflows/a.yml", DANGLING_WORKFLOW)
    write(
        tmp_path,
        ".github/workflows/b.yml",
        """
name: release
jobs:
  release:
    runs-on: ubuntu-latest
    outputs:
      tag: ${{ steps.tagger.outputs.tag }}
      digest: ${{ steps.tagger.outputs.digest }}
    steps:
      - id: other
        run: echo hi
""",
    )

    result = run(WORKFLOW_OUTPUTS, tmp_path)

    assert result.returncode == 1
    assert "a.yml: job resolve output flavors reads steps.matrix" in result.stderr
    assert "b.yml: job release output tag reads steps.tagger" in result.stderr
    assert "b.yml: job release output digest reads steps.tagger" in result.stderr


def test_outputs_not_built_from_step_references_are_accepted(tmp_path):
    write(
        tmp_path,
        ".github/workflows/build.yml",
        """
name: build
jobs:
  resolve:
    runs-on: ubuntu-latest
    outputs:
      image: ${{ inputs.image }}
      owner: ${{ github.repository_owner }}
    steps:
      - run: echo hi
""",
    )

    result = run(WORKFLOW_OUTPUTS, tmp_path)

    assert result.returncode == 0, result.stderr


def test_shipped_workflows_have_no_dangling_step_outputs():
    result = run(WORKFLOW_OUTPUTS, REPO_ROOT)

    assert result.returncode == 0, result.stderr
