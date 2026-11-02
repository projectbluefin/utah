---
name: upstream-audit
version: "1.0"
last_updated: "2026-11-02"
id: upstream-audit
one_line_purpose: Audit declared upstream pins without changing Utah's checkout.
entry_point: docs/skills/upstream-audit.md
category: ci-ops
mcp_compliance_level: partial
optimization_status: draft
status: active
dependencies: []
tags: [upstreams, freshness, dependencies, audit]
description: >-
  Read-only just audit-upstreams report for OCI inputs, Actions, submodules,
  Bluefin parity and pinned release tools, with explicit incomplete coverage.
metadata:
  type: procedure
---

# Read-only upstream freshness audit

`just audit-upstreams` is the host-side freshness report. It is distinct from
`audit-bluefin-parity` below: that older command partitions RPM availability;
freshness asks whether the declared external inputs have changed upstream.
It is deliberately outside `just check`, so ordinary validation does not
gain a dependency on live freshness metadata.

```bash
just audit-upstreams                         # terminal report
just audit-upstreams --json                  # one JSON document on stdout
just audit-upstreams --check                 # updates also fail the command
just audit-upstreams --json --timeout=15 --jobs=4
python3 scripts/audit-upstreams.py --json    # equivalent direct invocation
python3 -m unittest discover -s tests -p 'test_audit_upstreams.py'
```

The inventory is derived from tracked worktree manifests, image declarations,
workflow `uses:` references and pinned download scripts, not a second list of
versions. It reads gitlink pins from the index and tracking hints from
`.gitmodules`; extension sources need not be initialized. The package image
ARG and Renovate's `factory-pin:` stamp are reported together when they agree,
and separately when they differ. A pin bump must still update both.

Coverage includes digest-pinned base/package/Common/Brew/tacklebox images,
other declared external OCI images (including floating host/CI images), GitHub
Actions and reusable workflows at their declared version/branch hints,
extension gitlinks, OGC's release tag plus exact commit, NVIDIA's vendor
`latest.txt`, and GitHub release/archive tools and installer downloads.
Actions follow their explicit hint. GitHub's newest stable release is also
shown as a **review-only** candidate, without changing the declared branch or
major policy. `tracked_status` preserves the declared-channel result; a newer
fixed release or major can make the overall row `update` even when that
channel remains current. `release_review` records the release tag/commit and
whether it is newer. Lookup failures remain errors, rather than pretending
the old exact tag establishes full freshness.
Annotated tags are peeled to commits. OCI comparisons use the exact top-level
manifest/index digest returned with the same multiarch Accept headers: a
platform child digest is not treated as the identity of its parent index.
No image layers are read.

Bluefin's repository and content path come from `check-parity`. Freshness
compares the local manifest bytes with the **live upstream default branch**,
resolved through GitHub's declared `default_branch`, not merely the old SHA
in `.bluefin-parity-ref`. If content is unchanged while the branch advances,
it remains content-current; the candidate still records the branch head.
Sync changed content verbatim and move the parity pin together.

Every row provides `current`, `candidate`, `locations`, `status`, `detail`
and `next_step`; JSON also includes a schema version, timestamp, summary and
`coverage_complete`. `coverage.limitations` and `coverage.failed_lookups`
separate intentional unqueried inputs from failed requests; full row details
remain in `results`. `current` means only that the stated comparison matches,
not that the package is safe, compatible or runtime-accepted. `update` means a
different upstream candidate exists. `unknown` means the comparison is not
covered (for example a floating reference without an exact committed pin).
`error` means a lookup/parsing failure; failed requests never become current.
Exit codes are **0** for a complete report (known updates are allowed without
`--check`), **1** for known updates with `--check`, and **2** for incomplete or
error coverage in either mode. A partial network failure preserves other rows.

The report explicitly lists unversioned HTTP/RPM/Flatpak consumption endpoints
and the factory package-source universe as incomplete coverage. A matching
factory digest does **not** establish that each recipe, distro package,
Flatpak or transitive dependency is fresh. These limitations intentionally
keep the overall report incomplete and exit 2 until separately queried;
do not turn an image-level match into an all-packages-current claim.

Checkout metadata identifies local HEAD, branch, dirty paths, remote main's
live SHA and whether HEAD matches it. Inputs are the local worktree, not
necessarily remote main, and no finding about a dirty/older checkout should
be described as a finding about current main.

Only Python's standard library and read-only git commands are needed. Optional
`GH_TOKEN` or `GITHUB_TOKEN` authenticates GitHub API metadata requests; the
auditor never invokes login, writes tokens, or forwards that token to
registries/vendors. Anonymous registry bearer tokens stay in memory.
Requests have bounded timeouts and metadata sizes; concurrency is capped at
eight (default four), and identical requests, including failures, are
single-flight cached for the invocation. There are no image pulls, source
checkouts/updates, shell evaluation, builds, writes or persistent caches.
JSON still emits a complete error document when inventory discovery fails.

Use the suggested next steps to prepare reviewed pin/checksum updates, then
run the actual package-resolution, build and runtime/ISO gates. This audit
does not bypass required checks, exact-head trust, promotion holds or runtime
acceptance, and is a host helper, not installed into the composed image.
