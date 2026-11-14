# Packages and parity

Utah composes Hummingbird's `os-minimal` base with its package contract
through the Utah package factory. `/usr/share/utah/base.toml` is Utah's base
manifest (frozen from Bluefin's, edited by hand); `/usr/share/utah/utah.toml` contains Utah's
additions, `[parity]` provisions, and `[unavailable]` exceptions with tracking
issues. `/usr/share/utah/package-origins.json` and `package-origins.txt`
record package origins. Query the machine with `rpm -qa` and `rpm -qf PATH`.

Only Hummingbird's `public-hummingbird-x86_64-rpms` repository is enabled at
runtime. The package factory's build-time repository is disabled because its
mounted contents are absent. Fedora repositories are never enabled at runtime.
`dnf` therefore cannot resolve every package used to build the image.
Do not enable Fedora repositories or install packages into the read-only root
as a repair. Use Homebrew for user tools or rebuild the image for OS changes.

Checkout work: update `packages/base.toml`, `packages/utah.toml` or the
package factory; Dakota is the parity target (`baselines/GAP.md` in the
checkout), and unavailable packages keep their tracking issues.
Build, parity and repository checks require a checkout and network;
they are not commands to run from this installed bundle.
