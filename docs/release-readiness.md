# Utah release-readiness tracking (against Dakota)

This document tracks the work called out in projectbluefin/utah#24: "tracking: close Utah release-readiness gaps against Dakota".

Audit slices and related issues:

- projectbluefin/utah-packages#22
- projectbluefin/utah-packages#23
- projectbluefin/utah-packages#24
- projectbluefin/utah-packages#25
- projectbluefin/utah#11
- projectbluefin/utah#12
- projectbluefin/utah#13
- projectbluefin/utah#14
- projectbluefin/utah#15
- projectbluefin/utah#16
- projectbluefin/utah#17
- projectbluefin/utah#18
- projectbluefin/utah#19
- projectbluefin/utah#20
- projectbluefin/utah#21
- projectbluefin/utah#22
- projectbluefin/utah#23

Existing deferred-package issues:

- projectbluefin/utah#10
- projectbluefin/utah-packages#19
- projectbluefin/utah-packages#20
- projectbluefin/utah-packages#21

Acceptance criteria (from issue #24):

- [ ] Every linked P0/P1 slice is resolved or has an explicit maintainer-approved exception.
- [ ] A Utah image digest passes package, desktop, VM, installer, upgrade, and rollback gates.
- [ ] A checksummed production ISO passes offline plain and encrypted installation gates.
- [ ] Moving testing/stable tags and published ISO artifacts can be traced to the exact tested digest and workflow run.

Proposed next steps

1. Triage each linked issue and either resolve with a PR or seek an explicit exception from maintainers.
2. Add CI workflows that publish evidence (digests, workflow run IDs) for promoted artifacts.
3. Ensure ISO build workflow includes checksum artifacts and that the publish step traces back to the tested digest.
4. Verify and document upgrade and rollback test procedures in docs/ or tests/ and attach artifacts to the promoting workflow.

If you are working on this issue, update this file with the PRs or exceptions and mark checklist items accordingly.



— hive: backend=goose model=gpt-5-mini
