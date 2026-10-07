## About artwork and the panel icon (#318)

The About page's OS name is separate from its artwork. GNOME 51's
`setup_os_logo` uses `DISTRIBUTOR_LOGO` and `DARK_MODE_DISTRIBUTOR_LOGO` before
consulting os-release `LOGO`; Utah's spec configures Fedora's
`fedora_logo_med.png`/`fedora_whitelogo_med.png` paths, or the RHEL
`fedora-logo.png`/`system-logo-white.png` pair. Common ships Bluefin artwork
under all four names, but the logos-RPM swap erases the early overlay.
The desktop RUN bind-mounts Common's pinned pixmaps and branding restores
those four files after the RPM transaction without adding a COPY layer.
The package installer checks for residual distro artwork before this restore;
the desktop contract then checks the restored Bluefin assets.

The pinned command-menu extension creates `St.Icon` from `menuicon-setting`.
Keep Bluefin's `ublue-logo-symbolic`, command labels/order/location and help
URLs unchanged. The contract requires the SVG and the compiled hicolor cache, which branding
regenerates after all overlays. The menu must show the mark (not a
placeholder), retain commands, and show Bluefin artwork with the Utah OS name.
