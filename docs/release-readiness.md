# Utah release-readiness tracking (against Dakota)

Tracks the work called out in [projectbluefin/utah#24](https://github.com/projectbluefin/utah/issues/24):
close Utah's release-readiness gaps against Dakota. GitHub remains the
canonical source of history; this file is the one-glance status of where each
slice stands and what unblocks it.

**How this stays current:** updated by the release owner whenever any linked
issue or PR below changes state, and checked before every `testing`
promotion. If you land a PR that closes or progresses a slice, update its row
in the same commit.

## Slice status

State key: ✅ merged/closed · 🔵 open with a PR in flight · 🟡 open, no PR yet.

### Parity & factory packages

| # | Slice | State |
|---|---|---|
| utah-packages#22 | Publish factory repositories atomically after closure validation | ✅ |
| utah-packages#23 | Rebuild reverse dependencies in incremental factory runs | 🟡 |
| utah-packages#24 | Build the complete Bluefin multimedia override closure | 🔵 · feeds utah#12 |
| utah-packages#25 | Lock factory buildroots and require recipe provenance | 🟡 |
| utah#11 | Track Bluefin's effective package payload | 🔵 · PR #60 |
| utah#12 | Install and assert the factory multimedia transaction | 🟡 · blocked on utah-packages#24 |

### CI topology & promotion gates

| # | Slice | State |
|---|---|---|
| utah#16 | Adopt Dakota's PR validation vs merge-queue topology | 🔵 · PR #58 (this PR); duplicate #78 needs a maintainer pick |
| utah#13 | Gate testing tags on exact-digest VM E2E | 🔵 · overlapping PRs #70, #77 |
| utah#34 | Boot every flavor before advancing `:testing` | 🔵 · PR #34 |

### Installer

| # | Slice | State |
|---|---|---|
| utah#14 | Dakota-style offline ISO build, install, boot CI | 🔵 · competing PRs #41, #59, #65 |
| utah#15 | Validate encrypted offline installations | 🔵 · PR #67 |
| utah#22 | Harden and define supported live-media boot paths | 🔵 · PR #61 |
| utah#65 | Repoint live installer at tuna-os/bootc-installer | 🔵 · PR #65 |

### Desktop & runtime

| # | Slice | State |
|---|---|---|
| utah#18 | Make every flavor pass the runtime desktop contract | 🔵 · PR #34 (boots every flavor) |
| utah#19 | Validate Bluefin first-boot services/hooks at runtime | 🔵 · PR #62 |

### Upgrade & rollback

| # | Slice | State |
|---|---|---|
| utah#17 | Validate bootc upgrade and rollback lifecycle | 🔵 · PR #66 |

### Supply chain

| # | Slice | State |
|---|---|---|
| utah#20 | Pin and verify executable image and installer downloads | ✅ · covered by PR #48 (SHA-256 on the bootc-installer bundle) |
| utah#21 | Attest GNOME versions and final repository policy | 🔵 · PR #68 |

### Docs & architecture

| # | Slice | State |
|---|---|---|
| utah#23 | Describe Utah's architecture, parity, and installer status | 🔵 · PR #63 |

### Deferred package gaps (explicit exceptions / unavailable)

| # | Slice | State |
|---|---|---|
| utah#10 | parity: evolution-ews-core unavailable (Exchange plugin) | 🟡 · needs a maintainer decision |
| utah-packages#19 | fish needs vendored cargo build (F44 has no crate RPMs) | 🟡 |
| utah-packages#20 | tailscale needs Go 1.28+ or version pin | ✅ |
| utah-packages#21 | zsh ruby conflict in buildroot | ✅ |

## Cross-PR dependency notes

- **#16 topology → #58 / #78.** Two PRs implement the same change; a
  maintainer should pick one and close the other to avoid a merge conflict.
- **#14 offline ISO CI → #41 / #59 / #65.** Three overlapping ISO E2E
  harnesses. Needs a single implementation decision before the encrypted
  variant (#67) is stacked on top.
- **#13 exact-digest gate → #70 / #77.** Overlapping; dedupe to one.
- **#12 multimedia parity → utah-packages#24.** The factory closure is the
  dependency; no point asserting the transaction until the package set is
  final.

## Acceptance criteria status (from #24)

- [ ] Every linked P0/P1 slice is resolved or has a maintainer-approved
      exception. _In progress — all slices have owners/PRs except #10 and the
      utah-packages#19/#23/#25 trio, which lack PRs._
- [ ] A Utah image digest passes package, desktop, VM, installer, upgrade, and
      rollback gates. _Not yet — no single digest has passed all gate sets;
      needs #34 + #62 + #66 + the installer harness to land and run green on
      one digest._
- [ ] A checksummed production ISO passes offline plain and encrypted
      installation gates. _In progress — harnesses #41/#59/#65 plus encrypted
      #67 not merged._
- [ ] Moving testing/stable tags and published ISO artifacts can be traced to
      the exact tested digest and workflow run. _Not yet — requires #13 / #70
      / #77._

## Next steps

1. Resolve the three dedupe decisions above (#58 vs #78, #41/#59/#65, #70/#77)
   so reviewers stop re-reviewing competing implementations.
2. Unblock `main` (Containerfile:81 script-install step, PR #56) — several
   flavor/refactor PRs (#53, #40, #44, #64) sit behind it.
3. Close #10 (evolution-ews-core) with a maintainer decision — it is the one
   slice with no PR or explicit exception.
4. As each gate lands here, update the acceptance criteria and move the tags.