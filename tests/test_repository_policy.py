"""Behavioral tests for allowlisted DNF repository policy."""

from __future__ import annotations

import configparser
import importlib.util
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "verify-rpm-contract.py"
spec = importlib.util.spec_from_file_location("verify_rpm_contract_policy", SCRIPT)
verifier = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = verifier
spec.loader.exec_module(verifier)

REPO = "public-hummingbird-x86_64-rpms"
BASEURL = "https://packages.redhat.com/api/pulp-content/public-hummingbird/x86_64/"
ALLOWED = {REPO}
PINS = {REPO: (BASEURL,)}
SECURITY = {
    REPO: {"gpgcheck": "1", "repo_gpgcheck": "0", "sslverify": "1", "proxy": ""}
}


def policy(
    allowed: set[str] = ALLOWED,
    baseurls: dict[str, tuple[str, ...]] = PINS,
    options: dict[str, dict[str, str]] = SECURITY,
) -> verifier.RepositoryPolicy:
    return verifier.RepositoryPolicy(frozenset(allowed), baseurls, options)


def check_sections(parser, source, repo_policy):
    return verifier.check_repo_sections(
        parser, source, set(repo_policy.allowed),
        expected_baseurls=repo_policy.baseurls, expected_security=repo_policy.options,
    )


def scan_repository(directory, repo_policy):
    return verifier.verify_repository_policy(
        directory, set(repo_policy.allowed),
        expected_baseurls=repo_policy.baseurls, expected_security=repo_policy.options,
    )


def config(**options: str) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None)
    body = [f"[{REPO}]", "enabled=1", f"baseurl={BASEURL}"]
    body.extend(f"{name}={value}" for name, value in options.items())
    parser.read_string("\n".join(body))
    return parser


class RepositoryOptionPolicyTests(unittest.TestCase):
    def errors(self, **options: str) -> list[str]:
        return check_sections(
            config(**options), "hummingbird.repo", policy()
        )

    def test_approved_origin_and_security_options_pass(self) -> None:
        self.assertEqual(
            self.errors(gpgcheck="1", repo_gpgcheck="0", sslverify="1"), []
        )

    def test_equivalent_scheme_host_case_and_trailing_slash_pass(self) -> None:
        parser = configparser.ConfigParser(interpolation=None)
        parser.read_string(
            f"[{REPO}]\nenabled=1\n"
            "baseurl=HTTPS://Packages.RedHat.COM/api/pulp-content/public-hummingbird/x86_64\n"
            "gpgcheck=1\nrepo_gpgcheck=0\nsslverify=1\n"
        )
        self.assertEqual(
            check_sections(parser, "hummingbird.repo", policy()),
            [],
        )

    def test_every_url_in_a_multivalue_baseurl_must_be_pinned(self) -> None:
        parser = configparser.ConfigParser(interpolation=None)
        parser.read_string(
            f"[{REPO}]\nenabled=1\n"
            f"baseurl={BASEURL} https://user:secret@evil.invalid/mirror\n"
            "gpgcheck=1\nrepo_gpgcheck=0\nsslverify=1\n"
        )
        errors = check_sections(parser, "hummingbird.repo", policy())
        self.assertTrue(any("unpinned baseurl" in error for error in errors), errors)
        self.assertNotIn("secret", " ".join(errors))

    def test_a_metalink_beside_the_pinned_baseurl_is_rejected(self) -> None:
        parser = configparser.ConfigParser(interpolation=None)
        parser.read_string(
            f"[{REPO}]\nenabled=1\nbaseurl={BASEURL}\n"
            "metalink=https://evil.invalid/mirrors.xml\n"
            "gpgcheck=1\nrepo_gpgcheck=0\nsslverify=1\n"
        )
        errors = check_sections(parser, "hummingbird.repo", policy())
        self.assertTrue(any("metalink" in error for error in errors), errors)

    def test_each_security_sensitive_option_is_attested(self) -> None:
        changes = {
            "proxy": "https://proxy.evil.invalid:8080",
            "sslverify": "0",
            "gpgcheck": "0",
            "repo_gpgcheck": "1",
        }
        for name, value in changes.items():
            with self.subTest(option=name):
                actual = {
                    "gpgcheck": "1", "repo_gpgcheck": "0", "sslverify": "1"
                }
                actual[name] = value
                errors = self.errors(**actual)
                self.assertTrue(errors, f"changed {name} passed: {actual}")
                self.assertTrue(any(name in error or "proxy" in error for error in errors), errors)
                if name == "proxy":
                    self.assertNotIn("proxy.evil.invalid", " ".join(errors))

    def test_the_digest_pinned_local_repository_keeps_its_gpg_exception(self) -> None:
        local_id = "utah-packages"
        parser = configparser.ConfigParser(interpolation=None)
        parser.read_string(
            "[utah-packages]\nenabled=1\nbaseurl=file:///etc/utah-packages\n"
            "gpgcheck=0\nrepo_gpgcheck=0\nsslverify=1\n"
        )
        self.assertEqual(
            check_sections(
                parser, "utah-packages.repo",
                policy(
                    {local_id},
                    {local_id: ("file:///etc/utah-packages",)},
                    {local_id: {
                        "gpgcheck": "0", "repo_gpgcheck": "0", "sslverify": "1", "proxy": ""
                    }},
                ),
            ),
            [],
        )

    def test_an_allowlisted_id_needs_both_origin_and_security_pins(self) -> None:
        errors = check_sections(
            config(gpgcheck="1", repo_gpgcheck="0", sslverify="1"),
            "hummingbird.repo", policy(baseurls={}, options={}),
        )
        self.assertTrue(any("baseurl" in error for error in errors), errors)
        self.assertTrue(any("security policy" in error for error in errors), errors)

    def test_runtime_checks_sections_in_dnf_conf(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dnf = root / "etc/dnf"
            dnf.mkdir(parents=True)
            (dnf / "dnf.conf").write_text(
                f"[main]\nreposdir=/custom/repos\n\n[{REPO}]\n"
                f"enabled=1\nbaseurl={BASEURL}\n"
                "gpgcheck=0\nrepo_gpgcheck=0\nsslverify=1\n"
            )
            errors = verifier.verify_runtime_repository_policy(policy(), root=root)
        self.assertTrue(any("gpgcheck" in error and "dnf.conf" in error for error in errors), errors)

    def test_runtime_rejects_a_dnf_wide_proxy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dnf = root / "etc/dnf"
            dnf.mkdir(parents=True)
            (dnf / "dnf.conf").write_text(
                "[main]\nproxy=http://proxy.evil.invalid:8080\n"
            )
            errors = verifier.verify_runtime_repository_policy(policy(), root=root)
        self.assertTrue(any("DNF-wide proxy" in error for error in errors), errors)

    def test_runtime_scans_distro_reposdir_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repos = root / "etc/distro.repos.d"
            repos.mkdir(parents=True)
            (repos / "hummingbird.repo").write_text(
                f"[{REPO}]\nenabled=1\nbaseurl=https://evil.invalid/mirror\n"
                "gpgcheck=1\nrepo_gpgcheck=0\nsslverify=1\n"
            )
            errors = verifier.verify_runtime_repository_policy(policy(), root=root)
        self.assertTrue(any("unpinned baseurl" in error for error in errors), errors)

    def test_runtime_honors_a_custom_reposdir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dnf = root / "etc/dnf"
            dnf.mkdir(parents=True)
            (dnf / "dnf.conf").write_text("[main]\nreposdir=/custom/repos\n")
            repos = root / "custom/repos"
            repos.mkdir(parents=True)
            (repos / "hummingbird.repo").write_text(
                f"[{REPO}]\nenabled=1\nbaseurl=https://evil.invalid/mirror\n"
                "gpgcheck=1\nrepo_gpgcheck=0\nsslverify=1\n"
            )
            errors = verifier.verify_runtime_repository_policy(policy(), root=root)
        self.assertTrue(any("unpinned baseurl" in error for error in errors), errors)

    def test_shipped_manifest_and_repository_files_pass_offline_policy_check(self) -> None:
        self.assertEqual(
            verifier.verify_repository_policy_from_manifest(
                ROOT / "packages/utah.toml", check_mode=True
            ),
            [],
        )

    def test_manifest_cannot_authorize_proxy_or_disabled_tls_verification(self) -> None:
        for insecure in (
            'proxy = "http://proxy.invalid:8080"',
            'sslverify = "0"',
        ):
            with self.subTest(policy=insecure), tempfile.TemporaryDirectory() as tmp:
                overlay = Path(tmp) / "utah.toml"
                option, value = insecure.split(" = ", 1)
                security = {
                    "gpgcheck": '"1"', "repo_gpgcheck": '"0"',
                    "sslverify": '"1"', "proxy": '""',
                }
                security[option] = value
                overlay.write_text(
                    f'[repositories]\nallowed = ["{REPO}"]\n'
                    f'\n[repositories.baseurls]\n{REPO} = ["{BASEURL}"]\n'
                    f'\n[repositories.security."{REPO}"]\n'
                    + "".join(f"{name} = {entry}\n" for name, entry in security.items())
                )
                self.assertTrue(
                    verifier.read_repository_policy(overlay)[1],
                    "manifest should not approve a transport downgrade",
                )

    def test_manifest_cannot_pin_an_unencrypted_http_origin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            overlay = Path(tmp) / "utah.toml"
            http_baseurl = "http://" + BASEURL.removeprefix("https://")
            overlay.write_text(
                f'[repositories]\nallowed = ["{REPO}"]\n'
                f'\n[repositories.baseurls]\n{REPO} = ["{http_baseurl}"]\n'
                f'\n[repositories.security."{REPO}"]\n'
                'gpgcheck = "1"\nrepo_gpgcheck = "0"\nsslverify = "1"\nproxy = ""\n'
            )
            self.assertTrue(verifier.read_repository_policy(overlay)[1])

    def test_cli_check_rejects_an_insecure_allowlisted_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "bluefin.toml"
            manifest.write_text('[fedora]\npackages = []\n')
            overlay = root / "utah.toml"
            overlay.write_text(
                f'[repositories]\nallowed = ["{REPO}"]\n'
                f'\n[repositories.baseurls]\n{REPO} = ["{BASEURL}"]\n'
                f'\n[repositories.security."{REPO}"]\n'
                'gpgcheck = "1"\nrepo_gpgcheck = "0"\nsslverify = "1"\nproxy = ""\n'
            )
            (root / "hummingbird.repo").write_text(
                f"[{REPO}]\nenabled=1\nbaseurl={BASEURL}\n"
                "gpgcheck=0\nrepo_gpgcheck=0\nsslverify=0\n"
            )
            stdout, stderr = io.StringIO(), io.StringIO()
            with patch.object(sys, "argv", [str(SCRIPT), "--check", str(manifest), str(overlay)]), \
                    patch.dict(os.environ, {"IMAGE_FLAVOR": "main"}), \
                    redirect_stdout(stdout), redirect_stderr(stderr):
                code = verifier.main()
        self.assertEqual(code, 1)
        self.assertIn("sslverify", stderr.getvalue())

    def test_offline_check_covers_repo_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "hummingbird.repo").write_text(
                f"[{REPO}]\nenabled=1\nbaseurl={BASEURL}\n"
                "gpgcheck=0\nrepo_gpgcheck=0\nsslverify=1\n"
            )
            errors = scan_repository(root, policy())
        self.assertTrue(any("gpgcheck" in error for error in errors), errors)

    def runtime_errors(self, configs: dict[str, str], *, repo_options: str = "gpgcheck=1\nrepo_gpgcheck=0\nsslverify=1\n") -> list[str]:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repos = root / "etc/yum.repos.d"
            repos.mkdir(parents=True)
            (repos / "hummingbird.repo").write_text(f"[{REPO}]\nbaseurl={BASEURL}\n{repo_options}")
            for relative, text in configs.items():
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text)
            return verifier.verify_runtime_repository_policy(policy(), root=root)

    def test_dnf5_third_default_repository_directory_is_scanned(self) -> None:
        errors = self.runtime_errors({"usr/share/dnf5/repos.d/leak.repo": "[fedora]\nenabled=1\n"})
        self.assertTrue(any("Fedora" in error and "leak.repo" in error for error in errors), errors)

    def test_main_dropins_mask_by_name_and_dnf_conf_wins_last(self) -> None:
        configs = {
            "usr/share/dnf5/libdnf.conf.d/20-security.conf": "[main]\nsslverify=0\nproxy=http://untrusted\n",
            "etc/dnf/libdnf5.conf.d/20-security.conf": "[main]\nsslverify=1\n",
            "etc/dnf/libdnf5.conf.d/90-signatures.conf": "[main]\ngpgcheck=0\n",
            "etc/dnf/dnf.conf": "[main]\ngpgcheck=1\n",
        }
        self.assertEqual(self.runtime_errors(configs, repo_options=""), [])
        configs["etc/dnf/dnf.conf"] = "[main]\ngpgcheck=0\n"
        errors = self.runtime_errors(configs, repo_options="")
        self.assertTrue(any("gpgcheck" in error for error in errors), errors)

    def test_repo_override_globs_apply_after_repo_settings(self) -> None:
        configs = {"usr/share/dnf5/repos.override.d/20-security.repo": "[public-hummingbird-*]\nsslverify=0\nproxy=http://untrusted\n"}
        errors = self.runtime_errors(configs)
        self.assertTrue(any("sslverify" in error for error in errors), errors)
        self.assertTrue(any("proxy" in error for error in errors), errors)
        configs["etc/dnf/repos.override.d/20-security.repo"] = "[public-hummingbird-*]\nsslverify=1\nproxy=\n"
        self.assertEqual(self.runtime_errors(configs), [])
        configs["etc/dnf/repos.override.d/90-final.repo"] = f"[{REPO}]\ngpgcheck=0\n"
        errors = self.runtime_errors(configs)
        self.assertTrue(any("gpgcheck" in error and "90-final.repo" in error for error in errors), errors)

    def test_effective_package_check_alias_cannot_bypass_policy(self) -> None:
        configs = {"etc/dnf/repos.override.d/security.repo": "[public-hummingbird-*]\npkg_gpgcheck=0\n"}
        errors = self.runtime_errors(configs)
        self.assertTrue(any("gpgcheck" in error for error in errors), errors)
        configs["etc/dnf/repos.override.d/zz-last.repo"] = f"[{REPO}]\ngpgcheck=1\n"
        self.assertEqual(self.runtime_errors(configs), [])

    def test_gpgcheck_policy_expands_to_effective_metadata_check(self) -> None:
        errors = self.runtime_errors({"etc/dnf/dnf.conf": "[main]\ngpgcheck_policy=full\n"}, repo_options="gpgcheck=1\n")
        self.assertTrue(any("repo_gpgcheck" in error for error in errors), errors)
        self.assertEqual(self.runtime_errors({"etc/dnf/dnf.conf": "[main]\ngpgcheck_policy=full\n"}), [])

    def test_overrides_enable_existing_leaks_but_cannot_create_repositories(self) -> None:
        configs = {"etc/dnf/repos.override.d/all.repo": "[fedora*]\nenabled=1\n"}
        self.assertEqual(self.runtime_errors(configs), [])
        configs["usr/share/dnf5/repos.d/fedora.repo"] = "[fedora-44]\nenabled=0\n"
        errors = self.runtime_errors(configs)
        self.assertTrue(any("Fedora" in error for error in errors), errors)

    def test_libdnf5_user_dropin_proxy_is_not_ignored(self) -> None:
        errors = self.runtime_errors({"etc/dnf/libdnf5.conf.d/proxy.conf": "[main]\nproxy=http://untrusted\n"})
        self.assertTrue(any("DNF-wide proxy" in error for error in errors), errors)



if __name__ == "__main__":
    unittest.main()
