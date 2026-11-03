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

`report` is not a Utah override: it comes from Common and launches the Report a
Problem app, which resolves the target repository from the booted image's
`IMAGE_ID` itself (the app's routing includes `projectbluefin/utah`). Do not
re-add a local `report` recipe to redirect reports.
