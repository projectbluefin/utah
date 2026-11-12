**Summary:** Not for merge. This exists only to give projectbluefin/actions#750 (the setup-runner apt package cache) its consumer validation run. It points Utah's image build at that PR's commit, so this PR's CI builds Utah with the new setup-runner code. Pull-request builds publish, sign and attest nothing. Close this once actions#750 lands.

---

- Change: `.github/workflows/build.yml` pins `reusable-build.yml` (both call sites) to actions#750's head `03e2fcf6ba76325ad5da6d317fe4b88fc2b6bdc2`, in place of `46001bf9…`.
- What to look at: the build jobs' "Setup runner" step. On a first run the cache misses, so the step updates and installs online, then saves the cache. A re-run should restore and verify the cached package lists and install without contacting Ubuntu.
- The commit lives in the PR author's fork of projectbluefin/actions. GitHub resolves it through the fork network, which is fine for a throwaway validation run but is why this must never merge.
- Per projectbluefin/actions `docs/skills/consumer-validation.md`. If #750's head changes, re-pin and re-run.

🤖 Generated with [Claude Code](https://claude.com/claude-code)

https://claude.ai/code/session_01WcWm7qQa538Zjcuvv8jBtn
