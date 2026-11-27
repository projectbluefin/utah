# End-to-end verification (Post-Testing E2E)

Utah's install gate is the gnome-setup medium (`iso/raw/`,
`docs/adr/0001-image-based-installer.md`), not fisherman: the owner decided
that Utah installs with upstream GNOME STF gnome-setup and systemd's
installer tools (#1569). The fisherman ISO (`iso/live/`, `luks-e2e.sh`,
`lifecycle-e2e.sh`) and the tacklebox ISO are gone. The installed-system
checks `raw_e2e.py` took over from them stay in `iso/scripts/` and run from
there (#1591): `e2e-services.sh` (every `[services]` unit of the installed
contract), `verify-installed-flavor.py`, `verify-countme-runtime.sh`,
`extension-state.sh` and `fastfetch-ocr-match.sh`, next to the QEMU helpers
`qemu_monitor.py`, `plymouth-prompt.py` and `luks-unlock.py`.

`post-testing-e2e.yml` downloads the originating build's digest artifacts.
The final `dispatch-iso` build job invokes it with `workflow_dispatch`, not
`workflow_run`: the latter did not fire after our GITHUB_TOKEN-dispatched
build. Explicit dispatch is a documented exception to token recursion
prevention. The resolver waits up to five minutes for the dispatching build
to finish and still requires a successful conclusion before reading artifacts.
`scripts/resolve-e2e-inputs.py` rejects PRs, foreign repositories, failed runs,
wrong branches/workflows, mutable references, conflicting digests, and missing
flavors. The expected set comes from `scripts/flavors.py images`.

## The install gate

The `gnome-setup` job calls `image-installer.yml` once per flavor with that
flavor's exact tested digest, after `verify-provenance`. For each it builds the
live-capable layer and the raw medium, bakes the Flatpaks, shrinks it, and
`iso/raw/raw_e2e.py` drives it in QEMU with a software TPM: live boot
(`UTAH_LIVE_READY` on serial), the gnome-setup GUI install into LUKS2 with the
recovery key, first boot (TPM2 unlock, branded Plymouth), gnome-initial-setup,
the desktop and `bootc status`, then `bootc switch` to an earlier
`testing-<date>-<sha>` digest (recovery-key unlock without the TPM) and
`bootc rollback`. Every stage leaves a screenshot. SELinux stays enforcing;
failed units and denials fail a boot. The medium and disks carry test
credentials and are never uploaded. Details:
[image-installer](../../local-testing/references/image-installer.md).

### A live session that cannot draw the installer

Stage 2 waits for gnome-setup's first page through AT-SPI. Sometimes the live
session never gets that far, and the harness used to say only "no buttons,
check boxes or labels" and "first setup page never appeared in setup" (run
45927940366, attempt 1, 2026-11-27: the same digest passed its attempt 2, and
the mirror in `projectbluefin/dakota-iso` run 46036916466 failed the same
way).

What the guest's journal shows there: the session drew its desktop
(`01-live-session.png`), then gnome-shell stopped logging about a second after
it registered the display with GDM, the accessibility registry declared it
unresponsive and disabled it (`Disabling unresponsive app with pid 2233`),
and the session's two desktop portal units, started at the same moment, were
killed with "start operation timed out". gnome-setup, autostarted into the
session a moment earlier, blocked on its first portal call ("Cannot get portal
org.freedesktop.host.portal.Registry version: Timeout was reached") and never
opened a window, so the accessibility bus listed no application at all. The
portal is the victim, not the cause: the shell stopped first. Why the shell
stopped is not in the journal, which is why the coredumps matter.

The harness now says so instead of guessing. While it waits for the first
page it asks the guest whether the session's portal units failed; a portal
that failed is given `PORTAL_GRACE` seconds to come back (the installer can
open its window without a portal) and the stage then fails with the reason
rather than running out its whole 300 s budget. Every stage-2 failure also
writes `evidence/live-session-failure.txt`: the session's portal and
accessibility units with their results and journal, the shell's and the
installer's process state (including the shell's kernel stack), the system's
coredumps (`coredumpctl list`, `coredumpctl info` for gnome-shell and
gnome-setup), and the applications the accessibility bus lists. The whole
boot's journal is still saved as `journal-failure.txt`.

`promote-to-testing` needs `gnome-setup` and `testsuite` for every flavor
(#1329, #1569): a red or skipped leg skips it, so nothing is recorded and no
tag moves. Then it runs the ratchet described next. The job switches to an
earlier digest rather than upgrading from the previous `:testing`, so the
upgrade direction is covered by the testsuite's update scenarios, not here.

Every network step in the workflow (checkout, apt, GHCR login and pulls)
carries its own `timeout-minutes`, so a wedged mirror or registry fails as a
named step instead of holding the job to its 180-240 minute timeout. apt has
taken up to 31 minutes on these runners and still succeeded (job
135107979619), so apt steps get 40; checkout gets 10 and pulls 20.
`tests/test_e2e_workflow_limits.py` fails a network step without one.

**`:testing` is the latest E2E-green commit, not the branch tip.** Main takes
a merge about every hour (people, Renovate, the hive bot), and a
build plus this E2E takes longer than that, so `testing` has always moved on by
the time a run finishes. Promotion therefore never compares against the branch
tip. `promote-to-testing` runs `scripts/promotion-ratchet.py`, in order:

1. **`record`** posts one commit status per tested digest on the tested commit:
   contexts `e2e/post-testing/<image>` and `e2e/testsuite/<image>`, state
   `success`, description = the exact `sha256:` digest, plus the aggregate
   `e2e/post-testing`. A status is
   per commit and per digest, so a later run, a rebuild, or a newer commit never
   hides it ("last status wins" cannot happen). It is posted before the
   decision because it is true whatever the ratchet decides.
2. **`decide`** moves `:testing` only if the tested commit is the same as, or a
   descendant of, the commit behind the image `:testing` already points at
   (GitHub `compare`, `ahead`). The tip is never read. Outcomes, per flavor and
   then combined: no `:testing` yet or tested is ahead -> `promote`; the same
   commit under a newer digest (the nightly `sync-main-to-testing` rebuild of
   an unchanged branch) whose dated tag still names the tested digest ->
   `promote`, keeping `:testing` and `testing-<date>-<sha7>` in step for the
   release gate; tested digests already `:testing` -> `noop`; tested commit
   older, or a same-commit rebuild already superseded under its dated tag by a
   newer rebuild -> `refuse` with a `::warning` (out-of-order completion is
   expected, the run stays green and the evidence is kept); a commit that is
   neither an ancestor nor a descendant of the one `:testing` points at is a
   divergence -- under testing -> squash -> main -> force-reset the commit
   behind :testing is routinely squashed out of history (a squashed fix-up, a
   rebase that dropped the commit the image was built from, projectbluefin/utah#1033),
   so it can no longer be verified against. Only the newer side may win: if
   the tested commit is strictly newer than the ghost, `:testing` is
   re-baselined forward to it with a `::warning`; if the tested commit is
   off-line and no newer, it refuses as an out-of-order completion; an image
   not built from the tested commit, any registry/API error or unreadable
   label -> `::error`, job fails, nothing moves. Any one refusing flavor
   refuses the whole promotion.
3. **Move** the tags (`skopeo copy --all --preserve-digests`) only on
   `promote`.

So an E2E of commit X that is newer than the promoted commit promotes X while
`testing` is already several commits ahead, intermediate builds that were never
E2E'd are skipped, and a slow older run can never move `:testing` backwards.
Tag copies are sequential, not an atomic multi-tag transaction: a registry
failure part-way leaves some flavors on the new commit. Rerunning the job, or
the next descendant's E2E, completes it, because a flavor still behind sees the
tested commit as `ahead` and the flavors already moved are no-ops.

**Recovering the commit behind a published image.** Every build bakes
`org.opencontainers.image.version=testing-<yyyymmdd>-<sha7>` into the image
config (`just build-ghcr`) and publishes the image under that same tag; the
image digest carries the label. Images built after #1400 also set
`org.opencontainers.image.revision` to the building commit's full SHA, and
`just build-ghcr` fails if it differs from `HEAD`; after the push, the build
workflow's `verify_revision` job re-reads it from the registry by digest
(#1468, [kernel-and-build.md](kernel-and-build.md)). Older images keep the
revision inherited from the Hummingbird base, identical across Utah commits
(verified on `testing-20261024-028d3f4` and `-9acbde2`, both `a7eaec5...`), so
use the `version` label for them. `vcs-ref` is still inherited and unusable. The label is expanded to a full SHA through the GitHub API;
the dated tag is checked against the `:testing` digest as corroboration and
a mismatch (a same-day rebuild re-pushed the tag) is a warning, not a refusal,
because the label still names the commit.

**Serialisation and writers.** The workflow-level group `utah-iso-e2e`
(`cancel-in-progress: false`) runs one E2E at a time, so two promotions never
interleave, and GitHub keeps only the newest pending run, so a backlog collapses
to the latest commit. `promote-to-testing` is the only workflow that writes
`:testing` (`tests/test_promotion_workflows.py` asserts this); builds publish
only the dated tag (`publish_stream_tag: "false"`). The recording and tag steps
run only from `main`: the release gate trusts these statuses, and a dispatch
from another ref would run that branch's copy of this file. It still tests; it
cannot publish evidence or move a tag.

**Ghost baseline.** Under `testing` -> squash -> `main` -> force-reset, the
commit behind a published `:testing` image is routinely squashed out of
`main`'s history (a squashed fix-up, a rebase that dropped the commit the image
was built from, projectbluefin/utah#1033). A `:testing` commit that is neither
an ancestor nor a descendant of the tested commit -- GitHub `compare` reports
`diverged` -- is exactly that squashed-away line, and the old logic refused such
a promotion forever, freezing `:testing` on a commit the graph can no longer
name. The ratchet now orders the two commits by committer date and lets only the
newer side win: if the tested commit is strictly newer than the ghost, it is
re-baselined forward to it (the latest E2E-green commit) with a `::warning`, so
nothing moves off the verified line; if the tested commit is off-line and no
newer -- a late out-of-order completion or an old build replayed by hand -- it
refuses with a `::warning` and leaves the tags as they are. The `behind` case
(the tested commit is a strict ancestor of what `:testing` points at) is refused
first, so a re-baseline never moves a flavor backwards.

**Recovery.** If `:testing` ever points at an image whose label cannot be
read, every promotion fails closed until a maintainer retags `:testing` to a
known-good dated tag (`skopeo copy --all --preserve-digests
docker://ghcr.io/projectbluefin/<image>:testing-<date>-<sha>
docker://ghcr.io/projectbluefin/<image>:testing`); the ratchet resumes from
there.

**What the release checks.** See `release-cadence.md`: `execute-release.yml`
requires both the `e2e/post-testing/<image>` and `e2e/testsuite/<image>`
statuses for the digest `:testing` points at, on the commit that digest was
built from. Digests promoted before the testsuite gated promotion have only the
first, so they fail closed until a green run moves `:testing`.

**Verification record.** A separate least-privilege `documentation` job takes
the main flavor's `image-installer-utah` artifact (every stage's screenshot and
evidence), writes `provenance.json` (source commit, E2E run, image) and
force-pushes it as the single commit of the orphan `verification` branch, so
screenshots never accumulate in `main`'s history. It then proposes a README
PR (`scripts/update-e2e-readme.py`) whose evidence block shows
`14-first-boot-desktop.png`, pinned to that commit. The job token only pushes
the `verification` branch; trusted `main` alone mints a repository-scoped
Mergeraptor App token to open the PR, so its pull_request checks run. A
missing App secret fails this job visibly without deleting test artifacts.

For a deliberate rerun, dispatch Post-Testing E2E with `build_run_id` from a
successful testing build containing the current harness. Do not pass a PR
build: PRs do not publish immutable images. This matrix validates emulated
UEFI installation with a software TPM 2.0 (swtpm), not Secure Boot or
physical GPUs.

### Utah image provenance

Before the `gnome-setup` job pulls the new image, `verify-provenance`
runs `iso/scripts/verify-image-provenance.sh` on every digest in the matrix
(utah#1018). It verifies the SLSA provenance attestation published by the
reusable build workflow (`projectbluefin/actions` `reusable-build.yml`) for
source repository `projectbluefin/utah` on `refs/heads/testing`, so an image
attested by any other workflow or repository never reaches the privileged
e2e jobs. The `testsuite` job is not gated on it.

A 404 from `gh attestation verify` means the build never attested; it is
not a lookup problem. The reusable build's `Sign, publish SBOM, and attest`
step is `continue-on-error`, and inside it the provenance attestation runs
only if the SBOM steps before it succeeded. A green build can therefore ship
no attestation, and `:testing` stops advancing (2026-10-31 to 11-06). That
happened because Utah's `gen-sbom` recipe, run under `sudo`, left a root-owned
`sbom_out/` that the unprivileged SBOM step could not write. In the build log,
check `sign.gen-sbom` and `sign.__actions_attest-build-provenance`.

### Testing freshness alarm

A broken promotion gate never fails a build: `:testing` simply stops moving.
`testing-freshness.yml` runs every six hours. `scripts/testing-freshness.py`
takes the completion time of the "Move :testing to the tested digests" step
in the newest post-testing-e2e run whose `promote-to-testing` job ran it. That
job is the only writer of `:testing`, so the step's end is the promotion time.
It scans runs started up to 36 hours plus a day of slack ago; when the move
is older than 36 hours, or no scanned run moved the tag, the script opens one issue labelled `kind/bug`, `area/ci` and
`priority/critical-urgent` for the hive to pick up. Every run refreshes the
issue with the newest e2e run's failed jobs, and the issue closes itself once
the tag moves. Nothing on the image can say when it was promoted: the move is
`skopeo copy --preserve-digests`, and the config's `created` is
SOURCE_DATE_EPOCH (the last build-input commit). A run's overall conclusion is
not used either, because a run can promote and fail a later job, or pass while
the ratchet holds the tag. If the job or step is renamed, update
`scripts/testing-freshness.py`; `tests/test_testing_freshness.py` fails until
you do. The script finds its issue through the REST issues listing (label
`area/ci` plus a body marker), not issue search. Dispatch the workflow with
another `max_age_hours` to try a different threshold.

### Infra re-run

One red leg skips `promote-to-testing`, so a transient fault wastes the whole
run. In run 44538634534 every LUKS, lifecycle and production-ISO job passed,
and two testsuite legs failed on a GHCR `fetching blob: received unexpected
HTTP status: 500`. `rerun-e2e-infra-failures.yml` fires when a Post-Testing
E2E run completes with `failure` on `run_attempt` 1. `scripts/e2e-infra-rerun.py`
downloads every failed, timed-out or cancelled job's log and judges only the
step that printed the first `##[error]`, so a fault an earlier step recovered
from cannot excuse a later product failure. The signatures, each tied to the
run that showed it, live in `SIGNATURES` in the script: runner shutdown or
lost communication, a job with no log at all (HTTP 404/410), a registry 5xx,
a Sigstore 5xx, `All mirrors were tried` / `Librepo error: Curl error`,
`Could not resolve host: codeberg.org`, a GNOME GitLab 5xx on a `git clone`
(run 45673646964: 12 testsuite legs, the gnome-ponytail-daemon clone), and
`libpod_lock`. Add one only with a
run that shows it, and a fixture under `tests/fixtures/e2e-infra/`.

When every failed job matches, the `rerun` job runs `gh run rerun --failed`
once. One unexplained failure leaves the run red, and nothing re-runs an
attempt 2. After the wait, the job re-checks that the run is still on attempt 1
and is still the newest Post-Testing E2E run; a newer run supersedes it and
would otherwise lose its pending place in the `utah-iso-e2e` group. Classified
against the 26 failed runs between 2026-11-15 and 11-19 (215 failed jobs),
only 44538634534 would have been re-run.

The delay is the wait timer of the `e2e-infra-retry-wait` environment, not a
`sleep`, so the waiting job holds no runner. The environment must have a
20-minute wait timer (Settings -> Environments, or
`gh api -X PUT repos/projectbluefin/utah/environments/e2e-infra-retry-wait -F wait_timer=20`).
Without it, Actions creates the environment with no rules on first use and the
re-run starts at once; the classify job prints a `no re-run delay` warning
while that is so. Dispatch the workflow with a `run_id` to classify any run
without re-running it.

### Startup failures

A run GitHub refuses to start is created completed with zero jobs and
conclusion `startup_failure`: a deleted `gh-readonly-queue/*` ref or a
workflow file that does not parse. GitHub sends no `workflow_run` event for
it, so the infra re-run above never fires, and `:testing` stops until the
freshness alarm. `rerun-startup-failures.yml` polls every three hours instead
(ported from tuna-os/tunaOS `scripts/rerun-startup-failures.py`).
`scripts/rerun-startup-failures.py` looks at the newest run of
sync-main-to-testing (on `main`), build (on `testing`) and Post-Testing E2E
(on `main`). Only when that newest run is a startup failure with zero jobs
does it re-dispatch, the same way the chain does: sync with no inputs, build
with the current `testing` SHA, E2E with `build_run_id` set to the newest
`testing` build created before the lost run whose `dispatch-iso` job
succeeded, even if the build failed elsewhere (the API does not return a lost
run's inputs). The dispatch
becomes the newest run, so the next sweep skips it and a manual restart is
never duplicated; no state is kept. A run with any job is a real failure and
stays red. Each workflow starts the next one, so only the most upstream lost
workflow is dispatched; a lost run further down is logged as covered, and the
chain replaces it. A workflow it cannot read or dispatch, or an E2E
run with no source build, is logged as an error and fails the sweep after
every workflow has had its turn. Native Common candidate is not swept: its
`common_sha` comes from projectbluefin/common and a lost run does not keep it.
Dispatch the workflow with `dry_run` to see what it would re-dispatch.

## Testsuite reporting (exact-digest VM suites)

`post-testing-e2e.yml` runs the `testsuite` job asynchronously against each
flavor's exact `@digest` in the `projectbluefin/testsuite` QEMU VM. The digests
come from `needs.resolve.outputs.digests` and the suite mappings from
`scripts/flavors.py suites`.

This job surfaces scenario results and screenshots directly
in GitHub Actions, and gates `:testing` promotion: `promote-to-testing` needs it
(#1329).

## Extension startup states

At first login, poll for `ACTIVE` (or the older `ENABLED` spelling) within
the test's retry budget, rather than accepting the first non-empty state.
GNOME Shell's `loadExtension()` initially sets `ERROR` before awaiting
initialization; an activation state may also print as `UNKNOWN`. In #969,
E2E runs 40635543943 and 40700004838 saw these states with an empty
`GetExtensionErrors` result. A persistent non-active state must still fail,
with diagnostics preserved; do not toggle the extension to make it pass.
