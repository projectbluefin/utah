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
- `:testing` advances per green build, not on a clock: the tags move in
  `post-testing-e2e.yml`, after the LUKS ISO matrix and the production-ISO
  composition both pass. `promote-testing-to-main.yml` is the daily 04:00 UTC
  heartbeat, so `:testing` is at most a day behind `main` and `main` is at
  most a day behind the newest validated `testing` image.
- `:stable` moves in `execute-release.yml` on every promotion push to `main`,
  gated by `run_release_gate: true` over `smoke,common`. A weekly promotion
  rather than a per-promotion one is still an open question in #336 -- that is
  a maintainer policy call, not a code gap, and nothing in this tree should
  encode a guess at it.

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
