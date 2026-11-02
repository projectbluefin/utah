"""Behavioral boundaries of the host-only upstream audit; no network required."""
import base64
from concurrent.futures import ThreadPoolExecutor
import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/audit-upstreams.py"
spec = importlib.util.spec_from_file_location("audit_upstreams", SCRIPT)
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)
PIN = "a" * 40
NEXT = "b" * 40
DIGEST = "sha256:" + "a" * 64
NEW_DIGEST = "sha256:" + "b" * 64


def http_error(code):
    error = urllib.error.HTTPError("https://example.invalid/private-token", code, "failure", {}, io.BytesIO())
    error.close()
    return error


class InputParsingTests(unittest.TestCase):
    def inventory(self, files, links=None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tracked = {}
            for name, text in files.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text)
                tracked[name] = ("100644", PIN)
            tracked.update(links or {})
            return audit.discover(root, tracked)

    def test_split_images_stamp_duplicate_and_shell_defaults(self):
        files = {
            "Containerfile": f"ARG PACKAGE_IMAGE=ghcr.io/acme/packages\nARG PACKAGE_IMAGE_SHA={DIGEST}\nARG PACKAGE_IMAGE_REF=${{PACKAGE_IMAGE}}@${{PACKAGE_IMAGE_SHA}}\nFROM ${{PACKAGE_IMAGE_REF}} AS packages\n",
            "packages/factory.repo": f"# factory-pin: {DIGEST}\n",
            "renovate.json": json.dumps({"customManagers": [{
                "datasourceTemplate": "docker", "depNameTemplate": "ghcr.io/acme/packages",
                "currentValueTemplate": "latest", "matchStrings": ["# factory-pin: "]}]}),
            "iso/scripts/tool.sh": f'IMAGE="${{OVERRIDE:-ghcr.io/acme/tool:stable@{DIGEST}}}"\npodman run "$IMAGE"\n',
        }
        images = [item for item in self.inventory(files) if item["kind"] == "oci"]
        self.assertEqual(len(images), 2)
        packages = next(item for item in images if item["name"].endswith("/packages"))
        self.assertEqual(packages["current"], DIGEST)
        self.assertEqual(packages["tracking"], "latest")
        self.assertIn("packages/factory.repo:1", packages["locations"])
        self.assertGreaterEqual(len(packages["locations"]), 3)
        tool = next(item for item in images if item["name"].endswith("/tool"))
        self.assertEqual(tool["tracking"], "stable")
        self.assertEqual(tool["current"], DIGEST)

    def test_workflow_subpaths_and_submodule_index_pin(self):
        files = {
            ".github/workflows/build.yml": f"jobs:\n  build:\n    uses: acme/workflows/.github/workflows/build.yml@{PIN} # v2\n    steps:\n      - uses: actions/checkout@{PIN} # v5\n      - uses: ./local-action\n",
            ".gitmodules": '[submodule "extension"]\n path = vendor/ext\n url = https://github.com/acme/extension.git\n branch = v65\n',
        }
        inputs = [item for item in self.inventory(files, {"vendor/ext": ("160000", NEXT)}) if item["kind"] == "github-ref"]
        self.assertEqual(len(inputs), 3)
        workflow = next(item for item in inputs if item["name"].startswith("acme/workflows"))
        self.assertEqual(workflow["repo"], "acme/workflows")
        self.assertEqual(workflow["tracking"], "v2")
        extension = next(item for item in inputs if item["name"] == "vendor/ext")
        self.assertEqual(extension["current"], NEXT)
        self.assertEqual(extension["repo"], "acme/extension")

    def test_release_archive_vendor_and_installer_are_manifest_derived(self):
        files = {
            "Containerfile": 'ARG TOOL_VERSION=v1.2.3\nRUN curl "https://github.com/acme/tool/releases/download/${TOOL_VERSION}/tool.tar.gz"\n',
            "scripts/module.sh": 'VERSION="${OVERRIDE:-0.15.4}"\ncurl "https://github.com/acme/module/archive/refs/tags/v${VERSION}.tar.gz"\n',
            "iso/live/src/install-flatpaks.sh": 'INSTALLER_REPO=other/installer\nINSTALLER_VERSION="${OVERRIDE:-v2026.09.25-abc}"\ncurl "https://github.com/${INSTALLER_REPO}/releases/download/${INSTALLER_VERSION}/bundle.flatpak"\n',
            "scripts/nvidia.sh": 'driver_version="${OVERRIDE:-595.99.02}"\nrun="NVIDIA-Linux-x86_64-${driver_version}.run"\nurl="https://download.nvidia.com/XFree86/Linux-x86_64/${driver_version}/${run}"\n',
            "scripts/kernel.sh": f'OGC_TAG="${{OVERRIDE:-v7.1.8-ogc1}}"\nOGC_COMMIT="${{COMMIT_OVERRIDE:-{PIN}}}"\ngit clone --branch "$OGC_TAG" https://github.com/acme/linux.git /tmp/build\n',
        }
        inputs = self.inventory(files)
        releases = {item["name"]: item for item in inputs if item["kind"] == "github-release"}
        self.assertEqual(set(releases), {"acme/tool", "acme/module", "other/installer", "acme/linux"})
        self.assertEqual(releases["acme/module"]["current"], "v0.15.4")
        self.assertEqual(releases["acme/linux"]["commit"], PIN)
        nvidia = next(item for item in inputs if item["kind"] == "nvidia")
        self.assertEqual(nvidia["current"], "595.99.02")
        self.assertEqual(nvidia["tracking"], "https://download.nvidia.com/XFree86/Linux-x86_64/latest.txt")

    def test_dynamic_or_unversioned_inputs_are_not_silently_current(self):
        files = {"scripts/unversioned.sh": 'BASE="$(touch /tmp/must-not-run)"\ncurl https://vendor.invalid/install.sh\npodman run ghcr.io/acme/floating:latest\ngit clone https://github.com/acme/tool.git\n'}
        inputs = self.inventory(files)
        self.assertTrue(any(item["kind"] == "limitation" and "vendor.invalid" in item["name"] for item in inputs))
        self.assertTrue(any(item["kind"] == "limitation" and "source build" in item["name"] for item in inputs))
        floating = next(item for item in inputs if item["kind"] == "oci")
        self.assertEqual(floating["current"], "ghcr.io/acme/floating:latest")
        self.assertNotIn("BASE", audit.assignments(files["scripts/unversioned.sh"]))

    def test_malformed_pins_remain_explicit_incomplete_inputs(self):
        files = {
            "Containerfile": "ARG BASE_IMAGE=quay.io/acme/base:latest@sha256:bad\nARG PACKAGE_IMAGE=ghcr.io/acme/packages\nARG PACKAGE_IMAGE_SHA=broken\n",
            "packages/factory.repo": "# factory-pin: malformed\n",
            ".github/workflows/build.yml": "steps:\n  - uses: acme/tool@\n  - uses: acme/tool\n",
        }
        inputs = self.inventory(files)
        limitations = [item for item in inputs if item["kind"] == "limitation"]
        names = {item["name"] for item in limitations}
        self.assertIn("malformed OCI digest", names)
        self.assertIn("malformed OCI pin PACKAGE_IMAGE_SHA", names)
        self.assertIn("malformed factory-pin", names)
        self.assertIn("malformed action reference", names)
        self.assertIn("acme/tool", names)
        for item in limitations:
            self.assertEqual(audit.resolve(item, audit.Client(), Path("/unused"))["status"], "unknown")

    def test_parity_source_is_derived_from_existing_gate(self):
        files = {"Justfile": 'check-parity:\n    ref=$(tr -d x < packages/.bluefin-parity-ref)\n    curl "https://raw.githubusercontent.com/acme/desktop/${ref}/packages/base.toml"\n    if diff -u "$upstream" packages/bluefin.toml; then\n        echo current\n    fi\n',
                 "packages/.bluefin-parity-ref": PIN, "packages/bluefin.toml": "local"}
        parity = next(item for item in self.inventory(files) if item["kind"] == "parity")
        self.assertEqual(parity["repo"], "acme/desktop")
        self.assertEqual(parity["path"], "packages/base.toml")
        self.assertEqual(parity["current"], PIN)
        self.assertEqual(parity["local"], "packages/bluefin.toml")


class GitHubResolutionTests(unittest.TestCase):
    def test_nested_annotated_tag_is_peeled_to_commit(self):
        client = audit.Client()
        responses = {
            "repos/acme/tool/git/ref/heads/v1": http_error(404),
            "repos/acme/tool/git/ref/tags/v1": {"object": {"type": "tag", "sha": "tag1"}},
            "repos/acme/tool/git/tags/tag1": {"object": {"type": "tag", "sha": "tag2"}},
            "repos/acme/tool/git/tags/tag2": {"object": {"type": "commit", "sha": PIN}},
        }
        def api(path):
            value = responses[path]
            if isinstance(value, Exception):
                raise value
            return value
        with patch.object(client, "api", side_effect=api):
            self.assertEqual(client.ref("acme/tool", "v1"), PIN)

    def test_rate_limit_does_not_fall_back_to_another_ref(self):
        client = audit.Client()
        with patch.object(client, "api", side_effect=http_error(403)) as request:
            with self.assertRaises(urllib.error.HTTPError):
                client.ref("acme/tool", "main")
        self.assertEqual(request.call_count, 1)

    def test_tag_only_projects_use_numeric_stable_order(self):
        client = audit.Client()
        with patch.object(client, "api", side_effect=[http_error(404), [
            {"name": "v7.9.0-ogc2"}, {"name": "v7.10.0-ogc1"}, {"name": "v8.0-rc1"}]]):
            self.assertEqual(client.latest("acme/linux"), "v7.10.0-ogc1")
        with patch.object(client, "api", side_effect=[http_error(404), [{"name": "v1"}] * 100]):
            with self.assertRaises(audit.AuditError):
                client.latest("acme/linux")

    def test_ogc_moved_tag_cannot_be_current(self):
        client = audit.Client()
        item = audit.entry("github-release", "acme/linux", "v7.1.8-ogc1", "latest stable release", "kernel.sh:1", repo="acme/linux", commit=PIN)
        with patch.object(client, "latest", return_value=item["current"]), patch.object(client, "ref", return_value=NEXT):
            result = audit.resolve(item, client, Path("/unused"))
        self.assertEqual(result["status"], "update")

    def test_fixed_action_hint_exposes_newer_release_without_changing_channel(self):
        client = audit.Client()
        item = audit.entry("github-ref", "acme/action", PIN, "v3.10.1", "workflow.yml:1", repo="acme/action", check_releases=True)
        with patch.object(client, "latest", return_value="v4.0.0"), patch.object(client, "ref", side_effect=[PIN, NEXT]):
            result = audit.resolve(item, client, Path("/unused"))
        self.assertEqual(result["tracked_status"], "current")
        self.assertEqual(result["status"], "update")
        self.assertEqual(result["candidate"], PIN)
        self.assertEqual(result["release_review"]["commit"], NEXT)
        self.assertTrue(result["release_review"]["review_only"])

    def test_major_alias_at_latest_release_is_channel_current(self):
        client = audit.Client()
        item = audit.entry("github-ref", "acme/action", PIN, "v5", "workflow.yml:1", repo="acme/action", check_releases=True)
        with patch.object(client, "latest", return_value="v5.1.0"), patch.object(client, "ref", return_value=PIN):
            result = audit.resolve(item, client, Path("/unused"))
        self.assertEqual(result["status"], "current")
        self.assertFalse(result["release_review"]["newer_available"])

    def test_release_review_network_failure_preserves_channel_result(self):
        client = audit.Client()
        item = audit.entry("github-ref", "acme/action", PIN, "v5", "workflow.yml:1", repo="acme/action", check_releases=True)
        with patch.object(client, "latest", side_effect=http_error(503)), patch.object(client, "ref", return_value=PIN):
            result = audit.resolve(item, client, Path("/unused"))
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["tracked_status"], "current")
        self.assertEqual(result["candidate"], PIN)

    def test_parity_compares_live_branch_bytes_not_old_pin(self):
        client = audit.Client()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "parity.toml").write_bytes(b"old content")
            item = audit.entry("parity", "acme/desktop/base.toml", PIN, "upstream default branch", "Justfile:1", repo="acme/desktop", path="base.toml", local="parity.toml")
            responses = [{"default_branch": "testing"}, {"encoding": "base64", "content": base64.b64encode(b"new content").decode()}]
            with patch.object(client, "api", side_effect=responses) as api, patch.object(client, "ref", return_value=NEXT):
                result = audit.resolve(item, client, root)
            self.assertEqual(result["status"], "update")
            self.assertEqual(result["candidate"]["commit"], NEXT)
            self.assertIn("ref=" + NEXT, api.call_args_list[1].args[0])
            self.assertEqual((root / "parity.toml").read_bytes(), b"old content")


class RegistryAndTransportTests(unittest.TestCase):
    def test_top_level_index_identity_not_child_platform_identity(self):
        client = audit.Client()
        item = audit.entry("oci", "ghcr.io/acme/tool", DIGEST, "latest", "Containerfile:1")
        with patch.object(client, "manifest", return_value=(NEW_DIGEST, {"manifests": [{"digest": DIGEST}]})):
            result = audit.resolve(item, client, Path("/unused"))
        self.assertEqual(result["status"], "update")
        self.assertEqual(result["candidate"], NEW_DIGEST)
        with patch.object(client, "manifest", return_value=(DIGEST, {})):
            self.assertEqual(audit.resolve(item, client, Path("/unused"))["status"], "current")

    def test_manifest_body_and_registry_header_must_agree(self):
        client = audit.Client()
        body = b'{"schemaVersion":2}'
        expected = "sha256:" + hashlib.sha256(body).hexdigest()
        with patch.object(client, "fetch", return_value=(body, {"Docker-Content-Digest": expected})):
            self.assertEqual(client.manifest("quay.io/acme/tool", "latest")[0], expected)
        with patch.object(client, "fetch", return_value=(body, {"Docker-Content-Digest": DIGEST})):
            with self.assertRaises(audit.AuditError):
                client.manifest("quay.io/acme/tool", "latest")

    def test_registry_bearer_auth_reads_only_manifests_and_token(self):
        client = audit.Client(token="github-secret")
        body = b'{"schemaVersion":2}'
        challenge = http_error(401)
        challenge.headers = {"WWW-Authenticate": 'Bearer realm="https://ghcr.io/token",service="ghcr.io",scope="repository:acme/tool:pull"'}
        with patch.object(client, "fetch", side_effect=[challenge, (b'{"token":"registry-secret"}', {}), (body, {})]) as fetch:
            client.manifest("ghcr.io/acme/tool", "latest")
        calls = fetch.call_args_list
        self.assertIn("/manifests/latest", calls[0].args[0])
        self.assertIn("/token?", calls[1].args[0])
        self.assertEqual(calls[2].args[1]["Authorization"], "Bearer registry-secret")
        self.assertNotIn("github-secret", str(calls))

    def test_fetch_is_single_flight_and_caches_errors(self):
        client = audit.Client()
        started = threading.Event()
        release = threading.Event()
        def failing_open(*args, **kwargs):
            started.set()
            release.wait(2)
            raise http_error(503)
        def fetch():
            try:
                client.fetch("https://api.github.com/repos/acme/tool")
            except urllib.error.HTTPError as exc:
                return exc.code
        with patch.object(client.opener, "open", side_effect=failing_open) as request:
            with ThreadPoolExecutor(max_workers=3) as executor:
                futures = [executor.submit(fetch) for _ in range(3)]
                started.wait(2)
                release.set()
                self.assertEqual([future.result() for future in futures], [503, 503, 503])
            self.assertEqual(request.call_count, 1)

    def test_authorization_does_not_follow_cross_host_redirect(self):
        request = audit.urllib.request.Request("https://api.github.com/a", headers={"Authorization": "Bearer secret"})
        redirect = audit.NoCredentialRedirect().redirect_request(request, None, 302, "redirect", {}, "https://other.invalid/a")
        self.assertIsNone(redirect.get_header("Authorization"))


class ReportingTests(unittest.TestCase):
    def test_partial_failure_keeps_successful_results_and_never_claims_current(self):
        client = audit.Client()
        items = [audit.entry("oci", "ghcr.io/acme/good", DIGEST, "latest", "Containerfile:1"),
                 audit.entry("oci", "ghcr.io/acme/bad", DIGEST, "latest", "Containerfile:2")]
        def manifest(repository, reference):
            if repository.endswith("/bad"):
                raise http_error(503)
            return NEW_DIGEST, {}
        with patch.object(client, "manifest", side_effect=manifest):
            results = [audit.resolve(item, client, Path("/unused")) for item in items]
        self.assertEqual([item["status"] for item in results], ["update", "error"])
        self.assertEqual(audit.exit_code(results), 2)
        self.assertEqual(audit.exit_code(results, check=True), 2)
        self.assertNotIn("private-token", json.dumps(results))

    def test_report_separates_coverage_limits_from_failures_and_is_read_only(self):
        client = audit.Client()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Containerfile").write_text(f"ARG BASE_IMAGE=ghcr.io/acme/good:latest@{DIGEST}\n")
            (root / "packages").mkdir()
            (root / "packages/vendor.repo").write_text("baseurl=https://vendor.invalid/rpms/\n")
            tracked = {"Containerfile": ("100644", PIN), "packages/vendor.repo": ("100644", PIN)}
            before = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
            with patch.object(audit, "checkout_identity", return_value={"head": PIN, "dirty": True}), \
                    patch.object(audit, "tracked_files", return_value=tracked), \
                    patch.object(audit, "git", return_value="https://github.com/acme/os.git\n"), \
                    patch.object(client, "ref", side_effect=http_error(503)), \
                    patch.object(client, "manifest", return_value=(NEW_DIGEST, {})):
                report = audit.audit(root, client, jobs=2)
            after = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
        self.assertEqual(before, after)
        self.assertFalse(report["coverage_complete"])
        self.assertTrue(report["checkout"]["dirty"])
        self.assertEqual(report["summary"]["update"], 1)
        self.assertIn("https://vendor.invalid/rpms/", report["coverage"]["limitations"])
        self.assertIn("factory package sources", report["coverage"]["limitations"])
        self.assertEqual(report["coverage"]["failed_lookups"], ["checkout remote main"])
        self.assertEqual(audit.exit_code(report["results"]), 2)

    def test_exit_semantics(self):
        self.assertEqual(audit.exit_code([{"status": "current"}]), 0)
        self.assertEqual(audit.exit_code([{"status": "update"}]), 0)
        self.assertEqual(audit.exit_code([{"status": "update"}], check=True), 1)
        for status in ("unknown", "error"):
            self.assertEqual(audit.exit_code([{"status": status}]), 2)
            self.assertEqual(audit.exit_code([{"status": status}], check=True), 2)

    def test_json_is_one_document_even_if_inventory_fails(self):
        output = io.StringIO()
        with patch.object(audit, "audit", side_effect=audit.AuditError("unmerged index")), contextlib.redirect_stdout(output):
            code = audit.main(["--json", "--check"])
        report = json.loads(output.getvalue())
        self.assertEqual(code, 2)
        self.assertFalse(report["coverage_complete"])
        self.assertEqual(report["results"][0]["status"], "error")

    def test_dirty_older_checkout_identity_is_reported_without_refresh(self):
        calls = []
        def git(root, *args):
            calls.append(args)
            return {("rev-parse", "HEAD"): PIN + "\n",
                    ("rev-parse", "--abbrev-ref", "HEAD"): "old-branch\n",
                    ("status", "--porcelain=v1", "-z", "--untracked-files=normal"): " M Containerfile\0?? scratch\0"}[args]
        with patch.object(audit, "git", side_effect=git):
            identity = audit.checkout_identity(Path("/checkout"))
        self.assertEqual(identity["head"], PIN)
        self.assertTrue(identity["dirty"])
        self.assertEqual(identity["branch"], "old-branch")


if __name__ == "__main__":
    unittest.main()
