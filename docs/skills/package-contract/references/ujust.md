# Runtime ujust dependencies

Common's `00-entry.just` imports `60-custom.just` after the shared recipes
with duplicate recipes enabled, but earlier imports win at equal depth.
The Containerfile preserves Common's entry point as `00-common.just` before
installing Utah's local overlay. Utah's `00-entry.just` imports that file and
`60-custom.just` at the same depth, so Utah's custom recipes are shallower
than Common's defaults and take precedence. Common still supplies the default
command and unrelated recipes. Keep these overrides small and test them through
`just`, including import precedence, when changing Common's pin or runtime
dependencies. The required Common import deliberately fails if composition
forgets to preserve the original entry point.

For #394, `device-info` prints a local report when `fpaste` is missing and
only uploads after confirmation when it is available. Its temporary report is
private and removed on exit. `changelogs` keeps Common's image/repository
selection but prints Markdown directly when `glow` is absent; HTTP and parsing
errors must remain failures. Enrollment reports the unsupported capability
without running `sudo` or `mokutil`: Utah has no module-signing certificate,
and shipping one without signing the modules would not fix Secure Boot.
Signing and enrollment remain tracked by #395. Common's guarded
`check-idle-power-draw` stays unchanged until the factory supplies `powerstat`.
These fallbacks do not add packages or enable Fedora runtime repositories.

For #446, `report` overrides Common's `bonedigger-report` recipe so bug
reports route to `projectbluefin/utah` instead of falling through Common's
`ublue-image-repo` grammar. The override sets
`UBLUE_IMAGE_REPO_BIN=/usr/local/libexec/utah-image-repo`; that Utah-local
shim short-circuits every `utah*` name to `projectbluefin/utah` and forwards
every other name to Common's authoritative resolver (so non-Utah images
inheriting from this image still resolve correctly). The shim itself is
installed by `Containerfile` from `scripts/image-repo.sh` (alongside the
other `utah-*` helpers, under the same `<name>.sh` -> `utah-<name>`
rename) and listed in `just check`'s presence assertion. Its option
loop mirrors Common's exactly — `--` and the first non-option both end
option parsing — and the remaining positionals are forwarded verbatim,
so an empty `IMAGE_NAME` keeps its slot instead of promoting
`IMAGE_TAG` into it. `IMAGE_NAME` itself falls back to the `IMAGE_NAME`
environment variable the same way Common's resolver does
(`${1-${IMAGE_NAME-}}`), so callers that supply the name via the
environment (without a positional) still hit the `utah*` short-circuit
(#465); absent positionals are still omitted rather than synthesised
as empty, so the upstream env fallback also applies on the fall-through
path.

Two deliberate differences from Common's `report` recipe: the override sets
`BONEDIGGER_BRAND="🐦 Utah Bug Report"` so the prompt names Utah rather than
Bluefin, and it does not forward Common's `BONEDIGGER_VERSION` because
`bonedigger-report` never reads that variable and it is not in scope for a
Utah-local recipe. The `--list` description is kept on a single comment line
immediately above `[group('System')]`; `just` uses only that line, so the
explanatory block above it must stay separated by a blank line or `ujust
--list` would print an implementation-comment fragment instead.
