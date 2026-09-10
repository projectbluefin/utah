# Verification

Written by `just luks-test` (iso/scripts/luks-e2e.sh). Do not edit by hand:
the next passing run overwrites it.

Every image below is a QEMU screendump taken during that run, at the moment
the check beside it passed.

| | |
| --- | --- |
| Captured | 2026-09-06T18:12:30Z |
| Live ISO | `utah-live.iso`, 7.7G |
| Installed image | `ghcr.io/projectbluefin/utah:testing` |
| Root filesystem | btrfs on LUKS2, passphrase unlock |
| Live session | GNOME, wayland |
| Installed session | GNOME, wayland, user `utahtest` |

## What passed

1. The live ISO boots and its session reaches `graphical.target` with
   `gdm.service` active and `gnome-shell` running — not a text console
   that merely answers SSH.
2. The installer creates a LUKS2 volume and installs from the ISO's embedded
   container store, with no network.
3. The installed disk boots on its own, with no ISO attached.
4. Plymouth's passphrase prompt is answered and the root volume opens.
5. The system reaches the graphical target rather than an emergency shell.
6. The user logs in at the GDM greeter and gets a GNOME session.

## Screenshots

### The installed system, running
![fastfetch](screenshots/installed-fastfetch.png)

A terminal on the desktop this test built, reporting the OS, kernel and
desktop from inside it. Everything below is how it got there.

### Live session
![Live session](screenshots/live-desktop.png)

The desktop the ISO boots into, with the installer available.

### GDM greeter on the installed system
![Greeter](screenshots/installed-greeter.png)

After the encrypted root has been unlocked and the system has reached the
graphical target. This is what proves the boot did not stop at a console.

### Logged in
![Desktop](screenshots/installed-desktop.png)

`utahtest`'s GNOME session, entered by typing the password at the
greeter above.

