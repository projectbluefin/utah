"""The ISO build pipeline: Flatpak retries, fork PRs, concurrency, tooling removal."""
import re
import subprocess
import unittest

from test_iso_ci import ROOT


class FlatpakRetryTests(unittest.TestCase):
    """A Flathub timeout must not fail a whole flavor's end-to-end run.

    Run 35432516418 lost utah-gaming to a single timed-out object while pulling
    Firefox, three minutes into composing the ISO, with nothing wrong in the
    image. The installs are the largest network operation in the build and had
    no retry, while the curl beside them has had one all along.
    """

    SCRIPT = ROOT / "iso/live/src/install-flatpaks.sh"

    def drive(self, stub: str) -> subprocess.CompletedProcess:
        """Run the real retry_flatpak against a stub flatpak, with sleep off."""
        harness = f"""
        set -uo pipefail
        eval "$(sed -n '/^retry_flatpak() {{/,/^}}/p' {self.SCRIPT})"
        sleep() {{ :; }}
        attempts=0
        {stub}
        retry_flatpak install org.example.App >/dev/null 2>&1
        echo "rc=$? attempts=$attempts"
        """
        return subprocess.run(["bash", "-c", harness], capture_output=True, text=True,
                              cwd=ROOT)

    def test_a_transient_failure_is_retried_and_succeeds(self):
        result = self.drive('flatpak() { attempts=$((attempts+1)); [ "$attempts" -ge 3 ]; }')
        self.assertEqual(result.stdout.strip(), "rc=0 attempts=3", result.stderr)

    def test_a_longer_outage_is_retried_and_succeeds_on_the_fifth_attempt(self):
        # Run 36230660725 lost utah to dl.flathub.org timeouts on attempts
        # 1-3 spread over ~20 minutes: the retry budget is five attempts.
        result = self.drive('flatpak() { attempts=$((attempts+1)); [ "$attempts" -ge 5 ]; }')
        self.assertEqual(result.stdout.strip(), "rc=0 attempts=5", result.stderr)

    def test_a_persistent_failure_still_fails_after_five_attempts(self):
        # The point is resilience, not swallowing errors: a repository that is
        # genuinely gone must still fail the build.
        result = self.drive("flatpak() { attempts=$((attempts+1)); return 1; }")
        self.assertEqual(result.stdout.strip(), "rc=1 attempts=5", result.stderr)

    def test_every_network_install_goes_through_the_retry(self):
        script = self.SCRIPT.read_text()
        installs = [line for line in script.splitlines()
                    if line.startswith("flatpak install")
                    or line.startswith("retry_flatpak install")]
        self.assertTrue(installs)
        for line in installs:
            with self.subTest(line=line):
                self.assertTrue(line.startswith("retry_flatpak install"),
                                f"unretried network install: {line}")

    def test_every_retried_install_is_idempotent(self):
        # The retry is only safe if re-running it is a no-op for a ref that
        # already completed. Without --or-update, an attempt that installed the
        # app but still exited nonzero makes the next attempt fail with
        # "already installed" -- the retry would turn a flaky success into a
        # hard failure, which is the opposite of why it was added.
        script = self.SCRIPT.read_text()
        calls = re.findall(r"^retry_flatpak install.*?(?=\n\S|\Z)", script,
                           re.MULTILINE | re.DOTALL)
        self.assertTrue(calls)
        for call in calls:
            with self.subTest(call=call.splitlines()[0]):
                self.assertIn("--or-update", call)


class ForkPullRequestKernelCacheTests(unittest.TestCase):
    """A fork PR cannot publish the kernel cache, and must not go red for it.

    #154, #157 and #158 were all red on the same thing: the kernel compiled and
    the push then failed with "denied: installation not allowed to Write
    organization package", twenty minutes in, on a permission no contributor can
    be granted from a fork. Three PRs blocked by CI plumbing rather than by
    anything in their diffs.
    """

    WORKFLOW = ROOT / ".github/workflows/build.yml"

    def setUp(self):
        import yaml
        self.text = self.WORKFLOW.read_text()
        self.jobs = yaml.safe_load(self.text)["jobs"]

    def test_the_push_is_conditional_but_the_build_is_not(self):
        step = next(s for s in self.jobs["kernel_cache"]["steps"]
                    if s.get("id") == "cache")
        run = step["run"]
        # The compile is what validates a kernel change, so it always runs.
        self.assertIn("podman build --tag", run)
        build = run.index("podman build --tag")
        guard = run.index('if [ "${CAN_PUBLISH}" != "true" ]')
        push = run.index("podman push")
        self.assertLess(build, guard, "the build must not be behind the guard")
        self.assertLess(guard, push, "the push must be behind the guard")

    def test_can_publish_is_true_for_everything_that_is_not_a_fork_pr(self):
        step = next(s for s in self.jobs["kernel_cache"]["steps"]
                    if s.get("id") == "cache")
        env = step["env"]["CAN_PUBLISH"]
        # An empty head repository is a push, a schedule or a dispatch.
        self.assertIn("github.event.pull_request.head.repo.full_name == ''", env)
        self.assertIn("== github.repository", env)

    def test_the_flavored_builds_are_skipped_rather_than_left_to_fail(self):
        # Without this they would try to pull an image that was never pushed.
        self.assertIn("needs.kernel_cache.outputs.available == 'true'",
                      self.jobs["build_kernel"]["if"])
        self.assertEqual(self.jobs["kernel_cache"]["outputs"]["available"],
                         "${{ steps.cache.outputs.available }}")

    def test_a_cache_hit_still_reports_the_image_as_available(self):
        step = next(s for s in self.jobs["kernel_cache"]["steps"]
                    if s.get("id") == "cache")
        hit = step["run"].index("Cache hit")
        self.assertIn("available=true", step["run"][hit:hit + 200])

class ConcurrencyTests(unittest.TestCase):
    """A testing build must not be cancelled by the next one.

    build.yml on `testing` is what produces the images and dispatches the ISO
    end-to-end validation the release gate reads. Cancelling it throws away the
    evidence: on 2026-09-19 two testing builds were cancelled within two minutes
    (11:09 and 11:11) and the promotion that followed failed with
    E2E_LAST_STATUS: skipped.
    """

    def test_only_pull_requests_are_cancelled(self):
        import yaml
        build = yaml.safe_load((ROOT / ".github/workflows/build.yml").read_text())
        self.assertEqual(build["concurrency"]["cancel-in-progress"],
                         "${{ github.event_name == 'pull_request' }}")

    def test_the_group_still_separates_refs(self):
        # Sharing one group across refs would make a testing build queue behind
        # an unrelated PR's.
        import yaml
        build = yaml.safe_load((ROOT / ".github/workflows/build.yml").read_text())
        self.assertIn("github.ref", build["concurrency"]["group"])


class BuildToolingRemovalTests(unittest.TestCase):
    """The [build] toolchain must not ship (D1, docs/bluefin-package-gaps.md).

    configure-services.sh removes the extension build tooling after the build.
    Passing --no-autoremove kept the dependency closure (ninja-build,
    meson-srpm-macros, libsass, *-devel chains) in the image; the default
    remove cleans up dependencies orphaned by the transaction.
    """

    def setUp(self):
        import tomllib
        manifest = tomllib.loads((ROOT / "packages" / "utah.toml").read_text())
        self.build = manifest["build"]["packages"]
        self.script = (ROOT / "scripts" / "configure-services.sh").read_text()

    def remove_line(self):
        lines = [line for line in self.script.splitlines()
                 if "remove" in line and "dbus-devel" in line]
        self.assertEqual(len(lines), 1,
                         "expected exactly one build-tooling removal command")
        return lines[0]

    def test_every_build_package_but_unzip_is_removed(self):
        # unzip is in [parity] as well as [build]: Bluefin ships it to users.
        line = self.remove_line()
        for pkg in self.build:
            if pkg == "unzip":
                continue
            self.assertIn(pkg, line, f"{pkg} from [build] is not removed")

    def test_removal_cleans_the_dependency_closure(self):
        self.assertNotIn("--no-autoremove", self.remove_line(),
                         "removal must let dnf clean the orphaned build closure")
