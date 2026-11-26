## The boot splash is graphical and branded (#461)

Utah ships Bluefin's splash: the factory Plymouth, Fedora's `bgrt` default
theme (the spinner on the firmware logo), and common's raptor as the spinner
theme's `watermark.png`. Two Hummingbird-composition bugs and one installer
change each took part of it away; the first two stayed green in every earlier
check because the splash still unlocked the disk:

- **Text prompt on any machine with a serial port.** Hummingbird's
  `/usr/lib/bootc/kargs.d/00-base.toml` sets `console=ttyS0,115200n8`, and
  Plymouth that finds a serial console in `/sys/class/tty/console/active`
  "manages them with details forced": its text `details` plugin everywhere,
  so QEMU and every PC with a 16550 UART showed a bare
  `Please enter passphrase for disk root:` line. kargs.d is additive, so
  `system_files/shared/usr/lib/bootc/kargs.d/10-utah-plymouth.toml` adds
  `plymouth.ignore-serial-consoles` instead. Kernel and systemd still log to
  ttyS0; only the splash and prompt stay off it. Reproduce with
  `plymouth.debug=stream:/dev/ttyS0` and look for `serial consoles detected`.
- **No watermark.** Utah copies common before the package transaction, and
  Hummingbird's `fedora-logos` owns
  `/usr/share/plymouth/themes/spinner/watermark.png`, so the logos swap in
  `utah-install-packages` erased common's raptor with it.
  `configure-branding.sh` re-applies common's `usr/share/plymouth` tree from
  the pinned common image after the transaction (bind-mounted by the
  Containerfile) and `cmp`s the default theme's watermark against it.
- **No `rhgb` from the installer.** The image does not set `rhgb quiet`;
  the gnome-setup medium installs them at `bootc install` time
  (`iso/raw/medium.py` `INSTALL_KARGS`), and the block copy carries them to
  the installed entry. (fisherman, the previous installer, wrote them only
  with `extras.bootSplash` on, and every flavor once booted to the text
  prompt because Utah's catalog entry had none, #1129.)

Guards, in build order: the contract's `[branding].files` asserts the
watermark and `[configuration]` the karg file; `regenerate-initramfs.sh`
asserts what the initramfs plymouthd will actually draw from (theme, its
`ModuleName` plugin, both renderers, a label plugin, `Plymouth.ttf`, and the
watermark byte-identical to the image's); and the install e2e
(`iso/scripts/plymouth-prompt.py`, called from `iso/raw/raw_e2e.py`) requires
the prompt on screen with that watermark at the theme's position.

While Plymouth draws it holds the console (`plymouthd --attach-to-session`),
so systemd's lines stop reaching the serial log until GDM takes the display;
the e2e harnesses read `graphical.target` over SSH for that reason. To see the
splash without an install, boot the image's own `vmlinuz` and `initramfs.img`
with QEMU `-kernel`/`-initrd` against a throwaway LUKS2 file and
`rd.luks.name=<uuid>=root rhgb`, then `screendump` from the monitor.
