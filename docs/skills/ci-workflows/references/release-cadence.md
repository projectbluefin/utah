# Release cadence and factory pin

The cadence is RFC'd in #336. What runs today:

- Open pull requests against `main`, never `testing`. `sync-main-to-testing.yml`
  resets `testing` to `main` on every push to `main` and again nightly on its
  own `20 22 * * *` schedule, so a commit merged straight into `testing` is
  orphaned: #404 was lost this way and had to be re-landed. `build.yml` runs on
  every pull request and declares `push: branches: [testing]`, but that trigger
  is not how a `main` commit reaches the image tags: the sync pushes `testing`
  with the workflow's own `GITHUB_TOKEN`, and a `GITHUB_TOKEN` push starts no
  workflow. `sync-main-to-testing.yml`'s `build` job therefore dispatches the
  build explicitly (`gh workflow run build.yml --ref testing`) once the sync
  job returns, which is the path that actually produces the images.
- `:testing` is the latest E2E-green commit, not the branch tip, and it only
  moves forward. The tags move in `post-testing-e2e.yml`, after the LUKS ISO
  matrix and the production-ISO composition both pass, and only if the tested
  commit is the promoted commit or a descendant of it
  (`scripts/promotion-ratchet.py`; see `iso-e2e.md`). Merges to `main` while an
  E2E runs are safe and never block it: contribution is never paused to
  promote. An out-of-order older run is refused with a warning and cannot move
  `:testing` backwards. `promote-testing-to-main.yml` is the daily 04:00 UTC
  heartbeat that refreshes the squash PR from the `testing` branch; it is not
  what moves image tags.
- `:stable` is released when it is over a week old and `:testing` has moved
  on: the owner's rule is "if the :stable tag is over a week old and :testing
  passes, we promote it". `execute-release.yml` checks daily at 06:00 UTC, after
  the nightly cycle (22:20 sync, the testing build it dispatches, then
  `post-testing-e2e`, usually done by about 02:30) and the 04:00 promotion-PR
  heartbeat. It also runs on a promotion push to `main` or a manual dispatch,
  which release as before. A scheduled run has no head commit, so
  `scripts/release-trigger.py` decides it from the registry and the release
  history instead: for every flavor of `scripts/flavors.py releases` it reads
  the `:stable` and `:testing` digests (`skopeo inspect --no-tags`; without
  `--no-tags` skopeo lists every tag, over half a minute per image). A flavor
  with no `:stable` (a first release, or a new flavor) counts as stale and
  releases. If `:testing` equals `:stable` everywhere, nothing is new and
  nothing is released. Otherwise it releases only when `:stable` last moved
  more than seven days ago, and every outcome is a `::notice` saying which rule
  held it back. Whether `:testing` passes is still decided by the gates below,
  not here. "Last moved" is the completion of the reusable release's `Promote
  digests to target tags` step in the newest `execute-release.yml` run that
  ran it successfully, read in one GraphQL query per 100 runs (`checkType:
  ALL`, so a rerun cannot hide an earlier attempt that retagged). It is not the
  image config `created` (SOURCE_DATE_EPOCH, the last build-input commit; a
  `--preserve-digests` copy cannot carry a release time) and not the
  `testing-<yyyymmdd>-<sha7>` version label, which dates the build that was
  later released, per flavor and to the day. The retag step moves every flavor
  or fails, so one time dates them all; a failed step does not count, which
  keeps the oldest flavor's age in force. A scheduled release finishes some
  minutes after 06:00, so seven days later the check sees it as just under a
  week old: a chain of scheduled releases settles on an eight-day rhythm.
  An unreadable registry or Actions API fails `check-trigger` rather than
  guessing. A run is serialized by the `utah-stable-release` concurrency group.
  Every release, scheduled or not, then goes through the same two gates.
  First the `e2e-evidence` job runs `promotion-ratchet.py release-gate`: for
  each flavor it takes the digest `:testing` points at, recovers the commit it
  was built from, and requires the `e2e/post-testing/<image>` success status
  that `post-testing-e2e.yml` recorded for exactly that digest on exactly that
  commit. No such evidence, no release; the testing branch tip is never
  consulted, so landing commits during a release can neither block nor bless
  it. The release then retags from the commit-addressed
  `testing-<yyyymmdd>-<sha7>` tag that gate verified (not `:testing` again) and
  runs `run_release_gate: true` over `smoke,common` against the exact
  candidate digests, with no scenarios excluded. `gate_test_ref` pins the
  gate to the testsuite commit `post-testing-e2e.yml` validated (`test_ref`,
  #773), which is the same commit as the pinned testsuite
  `e2e.yml` workflow (#946); Renovate bumps all three together and a test
  enforces it. If a scenario group goes red
  for a reason outside this repo, scope it with `gate_extra_tags`
  (actions#655) plus the matching `extra-tags` in `post-testing-e2e.yml` and a
  tracking issue; never switch the whole gate off. A stale `:stable` whose
  `:testing` has no recorded E2E evidence for its digests fails the gate, so
  the scheduled release fails closed instead of skipping silently, and the
  next day's check tries again.
  The promotion PR's own gate (`promote-testing-to-main.yml`) keeps
  `run_e2e: false` on purpose: the pinned `reusable-promote-squash.yml` keys the
  E2E status to the `testing` branch tip, the tip-equality assumption this
  design removes. Turning it on needs the reusable workflow to accept the
  promoted commit as `head_sha` (projectbluefin/actions); until then the
  E2E requirement is enforced at release time by `e2e-evidence`.

`ARG PACKAGE_IMAGE_SHA` is the digest of `ghcr.io/projectbluefin/utah-packages`,
the RPM repository every image installs from. The built-in Dockerfile manager
does not discover this ARG indirection, so Utah's local regex manager reads
both that pin and `packages/utah-packages.repo`'s `# factory-pin:` stamp as
occurrences of one Docker dependency tracking `latest`.

Digest updates are grouped into one Renovate PR. Both occurrences must change
to the same digest: the stamp invalidates the package transaction's layer
cache, while the ARG selects the repository and labels its provenance. The
existing equality and full transaction checks remain the adoption gates.

The extraction and real replacement were exercised with Renovate 44.132.2;
the inherited local preset is `config:recommended`. This replaces the separate
scheduled factory updater rather than running two bots over the same pins.
No `image-versions.yml` alias or second literal pin is introduced. Renovate's
PR event runs normal CI; every update still requires independent review.
