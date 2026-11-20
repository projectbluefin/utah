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

`bluefin-cli` drives bluefin-cli instead of Common's `ublue-bling`
([terminal experience](../../desktop-contract/references/bluefin-cli.md)). It
toggles on the marker line bluefin-cli writes into the shell's rc file
(`# bluefin-cli shell-config`) and installs the `cli` bundle when it turns the
shell on. `toggle-user-motd` stays Common's: uwelcome is the greeting.

For #920, `install-debug-tools` is the on-demand path for diagnosing shell
hangs, chosen over shipping `gdb-headless`, `elfutils` and `sysprof-cli` in the
base image (about 23 MB for every user) or adding a debug flavor. It runs
`bootc usr-overlay` and installs the three packages from the enabled
Hummingbird repository; the overlay is transient, so the tools vanish on
reboot. The recipe re-executes itself under `sudo` when not root, and stops
with bootc's error when `/usr` cannot be unlocked instead of letting `dnf`
fail later on a read-only filesystem.

To diagnose a hung `gnome-shell` with these tools, first sample the main
thread with `sudo eu-stack -p <pid>` to see the native frames, then attach
`gdb` and call `gjs_dumpstack()` (exported by `libgjs`), which prints the live
JS stack to the journal. Take the PID from `pgrep -u "$USER" -x gnome-shell`,
since `pidof` also returns GDM's greeter shell. `kernel.yama.ptrace_scope` is
1, so attaching needs `sudo`. Calling `gjs_dumpstack()` while the main thread
is inside a GC slice is unsafe: check the backtrace has no `GCRuntime` or
`js::gc` frames first, and otherwise detach and retry.
