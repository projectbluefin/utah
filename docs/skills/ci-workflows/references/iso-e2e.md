# ISO end-to-end verification

`post-testing-e2e.yml` downloads the originating build's digest artifacts.
The final `dispatch-iso` build job invokes it with `workflow_dispatch`, not
`workflow_run`: the latter did not fire after our GITHUB_TOKEN-dispatched
build. Explicit dispatch is a documented exception to token recursion
prevention. The resolver waits up to five minutes for the dispatching build
to finish and still requires a successful conclusion before reading artifacts.
`scripts/resolve-e2e-inputs.py` rejects PRs, foreign repositories, failed runs,
wrong branches/workflows, mutable references, conflicting digests, and missing
flavors. The expected set comes from `scripts/flavors.py images`.

Each configured image is pulled by digest, composed into a disposable debug
ISO, and passed to the existing `iso/scripts/luks-e2e.sh`. Both guests have
restricted networking. CI requires KVM, PNG screenshots, and OCR evidence
that fastfetch ran in the graphical terminal. The live-boot gate observes the
`UTAH_LIVE_READY` ready marker on the serial console in addition to
`graphical.target`: the marker is written only by the `utah-live-ready.service`
oneshot that runs after `display-manager.service`, so it catches a boot that
stopped just short of a usable display. Test credentials are confined to the
disposable ISO/disk; neither is uploaded or released.

The installed disk's first boot must show Plymouth's graphical passphrase
prompt with the payload's watermark (`iso/scripts/plymouth-prompt.py`:
tesseract finds the prompt text, and the default theme's `watermark.png`,
read from the live guest, matches at the theme's alignment). A prompt on the
serial console fails at once as the text fallback (#461); the judged frame is
kept as `installed-luks-prompt.png` and published with the other evidence.
The harness therefore keeps `rhgb` on the installed entry and drops only
`quiet`; Plymouth then holds the console until GDM starts, so the graphical
target is read over SSH as well as from serial.

Only after the entire LUKS matrix succeeds, `production-iso` composes a fresh
`DEBUG=0` x86_64 UEFI ISO from each same digest, writes its SHA-256 checksum,
and retains both as a 3-day Actions artifact. It is deliberately an artifact,
not a release: destination, flavor policy, naming, signing, and Secure Boot
are product decisions tracked by #186. Production-ISO composition is a
promotion prerequisite, so a failed production build cannot advance testing
tags. Debug ISOs and guest disks are never uploaded because they contain test
credentials.

**"Production" names the `DEBUG=0` build, not shippable media.** `DEBUG=0`
does exclude the test credentials and sshd path (`iso/live/src/configure-live.sh`),
so the retained artifact carries no secrets — but `iso/scripts/build-iso.sh`
hard-codes `enforcing=0 console=ttyS0,115200n8` on the boot entry for every
`DEBUG` value (the documented Issue #22 exception: rootless `podman unshare`
cannot write `security.selinux` xattrs into the squashfs root), so this ISO
boots SELinux-permissive with a serial console.
That was unremarkable while ISOs were disposable; retaining them under the
name "production" makes it worth stating plainly. Running
the live environment permissive is a product decision, and it belongs to #186
along with signing and Secure Boot — it must be settled there before any of
these ISOs reach users. Do not treat a green `production-iso` job as evidence
that the media is release-ready.

Each retained ISO is multi-GB (5.5 GB on average; the size budget in
`iso/scripts/build-iso.sh` is the ceiling), per flavor, per dispatch. At 30-day
retention that was 617 artifacts and 3.38 TB on 2026-11-20, nearly all
superseded by newer runs; the last three days alone held 88 (497 GB), which is
the steady state at `retention-days: 3`. Download an ISO you need within three
days, and if the matrix widens, revisit the window before the flavor count.

Every network step in the workflow (checkout, apt, GHCR login and pulls)
carries its own `timeout-minutes`, so a wedged mirror or registry fails as a
named step instead of holding the job to its 180-240 minute timeout. apt has
taken up to 31 minutes on these runners and still succeeded (job
135107979619), so apt steps get 40; checkout gets 10 and pulls 20.
`tests/test_e2e_workflow_limits.py` fails a network step without one.

Screenshot evidence identifies the tested image, not necessarily the E2E
workflow checkout. Validate `source_sha` against `build_run` and the published
image labels, then match the recorded digest to the E2E matrix job. A dispatch
can run newer verifier code against an older build; differing workflow and
image SHAs alone do not invalidate the evidence. Never treat those screenshots
as runtime proof for a later image commit. An image build passing its desktop
contract does not prove first-login GUI installation: ISO composition or boot
failure still blocks that acceptance and tag promotion.

Every matrix job preserves build/test logs, serial logs, and screenshots,
including on failure. Only passing jobs upload `docs/verification` with the
source commit, original build run, E2E run, image digest and ISO checksum.
The promotion job depends on the LUKS, production-ISO and testsuite matrices
(#1329): a red or skipped leg skips it, so nothing is recorded and no tag
moves. Then it runs the ratchet described next.

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
image digest carries the label. Do not use `org.opencontainers.image.revision`
or `vcs-ref`: they are inherited from the Hummingbird base image and identical
across Utah commits (verified on `testing-20261024-028d3f4` and `-9acbde2`,
both `a7eaec5...`). The label is expanded to a full SHA through the GitHub API;
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

**Upgrade and rollback gate.** The `lifecycle` job proves a booted machine
can move from the last promoted build to this one and back. `resolve` reads the
digest `:testing` serves before the run (the baseline) and compares it with the
run's `utah` digest (the candidate). The job builds a debug ISO of the baseline,
then runs `iso/scripts/lifecycle-e2e.sh`: install the baseline, `bootc switch`
to the candidate, reboot into it with a graphical check, roll back, and reboot
into the baseline. It runs for `utah` on the default install backend only; a
composefs entry can join the matrix after #1131. The job summary names both
digests and the `lifecycle-utah-ostree` artifact holds the phase JSON,
`lifecycle-summary.json` and screendumps. `promote-to-testing` and
`documentation` need it. When `:testing` already serves the candidate, or does
not exist yet, `resolve` sets `lifecycle=skip`, the job is skipped (not passed)
with a notice, and only that deliberate skip lets promotion go ahead; a failed,
cancelled or upstream-skipped lifecycle job blocks it.

**What the release checks.** See `release-cadence.md`: `execute-release.yml`
requires both the `e2e/post-testing/<image>` and `e2e/testsuite/<image>`
statuses for the digest `:testing` points at, on the commit that digest was
built from. Digests promoted before the testsuite gated promotion have only the
first, so they fail closed until a green run moves `:testing`.

A separate least-privilege job publishes the main desktop's record to a
`verification` branch and proposes a documentation PR that relinks the README
evidence block to it. The branch is an orphan: each passing run force-pushes it
as a single commit holding only the newest record, so screenshots never
accumulate in `main`'s history and clones stop growing. It also depends on
`production-iso`, so a failed production-ISO composition blocks this relink PR
too, not just testing-tag promotion — the record still has to wait on the whole
matrix composing cleanly. It updates only the README evidence link, preserving
the current README's other content; the superseded `docs/verification/` path is
replaced by the branch. The README links to the whole run, which also holds the
independent testsuite legs; those do not gate the record and may still be
running or red, so the README says so without predicting either outcome. The
evidence update requires the LUKS and production-ISO jobs to pass.
The job token only pushes the `verification` branch. Trusted `main` alone
mints a repository-scoped Mergeraptor App token (contents and pull-request
write) to open the PR, so its normal pull_request checks run and Hive's
review can land it like other bot proposals. A missing App secret or
installation fails this job visibly, without deleting test artifacts. The job
does not arm native auto-merge or imply a fresh pass for a different commit.

For a deliberate rerun, dispatch Post-Testing E2E with `build_run_id` from a
successful testing build containing the current harness. Do not pass a PR
build: PRs do not publish immutable images. This matrix validates emulated
UEFI desktop installation, not Secure Boot, TPM unlock, or physical GPUs.

The ISO size is bounded in `iso/scripts/build-iso.sh`: it fails closed above
the 8 GiB `ISO_MAX_GB` default (override per-run with `UTAH_ISO_MAX_GB`) once
the ISO is written. Historical post-fix E2E run `35469913325` measured 3.9G
(utah), 4.6G (gaming), 5.2G (nvidia), and 5.3G (nvidia-gaming). Diagnostic
run `38175242606` measured 4.7G, 5.5G, 6.0G, and 6.1G at the production zstd
level 3; the nvidia-gaming overage (98.8 MiB over) is #684. The opt-in review
VM matrix sets `UTAH_ISO_COMPRESSION_LEVEL=15` only to reach install/boot
checks; local `just iso` and production callers retain level 3 and the 8 GiB
guard. The guard was raised from 6 GiB after run `40107791411` measured the
main tacklebox ISO at 7,288,455,168 bytes (6.79 GiB; the budget had already
been exceeded, 6,928,269,312 bytes, before the GNOME app and parity packages
landed). The post-testing E2E applies the same limit to that ISO. Size
reduction strategies are tracked in #875; move the guard down again only with
a measured reduction. `just measure-iso` measures the squashfs root under each
store/compression strategy (see `evidence/iso-size.md`).

When the guard trips, measure before trimming. The kernel cache image's last
layer is only `/utah-cache`, so its archives can be inventoried without a
build: fetch that one blob from GHCR and `tar -tvf` the archives inside it.
That is how #684 was located: `ogc.tar` carried a 286 MiB
`/usr/src/linux-<ogc>` external-module build tree (228 MiB of it `arch/x86`
built objects and boot images) that gaming images unpacked and never removed.
To estimate the ISO effect, compress the candidate files in independent
128 KiB blocks with zstd level 3 -- `mksquashfs -b 131072 -comp zstd
-Xcompression-level 3` is what the ISO does -- rather than streaming them;
the tree came out at about 150 MiB that way, against the 98.8 MiB overage.

For plain `nvidia` (#740, measuring 6,640,365,568 B / ~6.18 GiB in diagnostic
run 38429837410 against the 6 GiB ceiling), the overage was driven by the
generic (`--no-hostonly`) initramfs for distribution kernel `7.2.9`. Because
neither `regenerate-initramfs.sh` nor `iso/live/Containerfile` specified a
compression algorithm, dracut defaulted to gzip, producing a ~240 MiB
initramfs. This ~240 MiB archive was duplicated four times across the offline
ISO media (inside `efi.img` ESP, in `images/pxeboot`, in the live squashfs root,
and in the embedded `/usr/lib/containers/storage` layer). Switching both
dracut invocations to `--zstd --reproducible` shrinks each archive from
~240 MiB to under ~60 MiB, saving ~360 MiB outside squashfs and over 100 MiB
inside squashfs, bringing plain `nvidia` comfortably below the 6 GiB budget
without modifying the kernel cache tag.

### Tacklebox image pin

The privileged tacklebox builder (`iso/scripts/build-iso-tacklebox.sh`) and
the tacklebox ISO verifier step in `post-testing-e2e.yml` both read
`config/tacklebox-image`, a single digest pin tracked by Renovate. Both reject
a mutable tag before running the container. Updating only the builder default
leaves verification executing unrelated code with root privileges; keep both
consumers on this same file.

### Tacklebox builder provenance

Digest-locking stops a tag move but not an untrusted digest landing in the
pin, so both consumers also verify the image before the pull (utah#918).
`iso/scripts/verify-tacklebox-attestation.sh` checks the image read from
`config/tacklebox-image` (or a digest-pinned `TACKLEBOX_IMAGE` override); it
does not hold the pin itself. It installs the version-pinned cosign binary
(verified against a pinned SHA-256) and runs
`cosign verify-attestation --type=https://slsa.dev/provenance/v1` with the
Fulcio identity pinned to the tacklebox `release-image.yml` workflow on
`refs/heads/main` or a `refs/tags/v*` tag (the upstream release triggers), so
an unsigned image or one attested by any other workflow or repository is
rejected before each Renovate-driven digest bump can run privileged.

### Tacklebox live-boot gate

`tacklebox-iso` boots its ISO with `iso/scripts/tacklebox-boot-gate.sh` and
passes when `UTAH_LIVE_READY` reaches the serial log. The gate polls for the
marker itself and returns as soon as it appears; a kernel panic or a QEMU
process that exited fails at once, and only a boot showing neither waits out
`UTAH_ISO_BOOT_TIMEOUT` (600 s in CI). It stops the VM on every exit path,
and the job uploads `serial.log` with the diagnostics.

Two mistakes made every run cost 30 minutes. The gate left its loop only on a
systemd `Reached target` line and checked for the marker afterwards; in runs
44538634534 and 44604822618 that line never ended the loop, so the step ran
the whole 1800 s timeout (30m03s each) and then passed. The job also never
granted `/dev/kvm`: GitHub-hosted runners do not make it readable by the job
user, so the gate logged `No /dev/kvm; falling back to TCG` and booted
emulated. Every job that runs
QEMU (`luks`, `lifecycle`, `tacklebox-iso`) runs `sudo chmod a+rw /dev/kvm`
in its "Prepare ISO and QEMU host" step, and `tests/test_iso_ci.py` asserts
that. Under KVM the `luks` job's live boot reaches the marker in about
35 seconds.

### Utah image provenance

Before `luks` and `tacklebox-iso` pull the new image, `verify-provenance`
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
`Could not resolve host: codeberg.org`, and `libpod_lock`. Add one only with a
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
successful `testing` build created before the lost run whose `dispatch-iso`
job succeeded (the API does not return a lost run's inputs). The dispatch
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

This job surfaces scenario results, fastfetch OCR logs, and screenshots directly
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
