# Diagnose the running image

Collect read-only evidence first:

```sh
cat /etc/os-release
cat /usr/share/ublue-os/image-info.json
bootc status
uname -a
findmnt /
systemctl --failed
journalctl -b -p err
journalctl --user -b
rpm -qa
```

`BUILD_ID` is the Utah source commit suffix (`IMAGE_VERSION` also carries
the version and, in published builds, the suffix); resolve it in
https://github.com/projectbluefin/utah, not in Hummingbird or Common.
`build-source.Containerfile` records the exact pinned base and source image
digests. These are build inputs; `bootc status` identifies the deployment.
A local build may override those defaults, so retain its build invocation too.

For a hung Shell, from a terminal or SSH use `pgrep -a -u "$USER" gnome-shell`,
`ps -L -p PID -o pid,tid,pcpu,stat,wchan:32,comm`, and
`journalctl -b _COMM=gnome-shell`. Save output under your home directory.

Known hang (Utah #354): on some hardware GNOME Shell stops responding while
someone types into the Wi-Fi password dialog. The journal shows NetworkManager
`need-auth` for the network, then `at-spi2-registryd` reporting
`Disabling unresponsive app` for the Shell's PID; the kernel keeps logging.
Connect without the dialog: `sudo nmcli --ask device wifi connect SSID`, from
a terminal, a text console (Ctrl+Alt+F3) or SSH. Collect the stacks below
before restarting the Shell and attach them to that issue.

Check availability before using optional samplers:

```sh
for tool in bootc journalctl ps top gnome-extensions gdb eu-stack strace perf sysprof-cli; do
    command -v "$tool" || true
done
rpm -q procps-ng gdb-headless elfutils strace perf sysprof-cli
```

The installed RPM database and command lookup are the tool inventory; this
bundle does not promise optional tools from a newer image (see Utah #920).
If `gdb` exists, a short `gdb -p PID -batch -ex 'thread apply all bt'`
can capture stacks, but attaching pauses the process and may need elevated
permission. Get consent before attaching to someone else's session.
Do not kill the Shell before collecting evidence: that loses the hang.
Homebrew's tools may be in `/home/linuxbrew/.linuxbrew/bin`; `sudo` does not
inherit that PATH. Use a verified absolute executable path when elevation is
needed, rather than changing root's PATH.

`bootc rollback` selects the previous deployment for the next boot; it does
not repair user configuration. Inspect `bootc status` first, save work, and
get the owner's agreement before rollback or reboot. Do not write to `/usr`:
composefs makes `/` read-only, and 100% usage there is expected. Use the
per-user extension override in the desktop skill for a reversible experiment.
