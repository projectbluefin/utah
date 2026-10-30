# Source and issue map

| Source | Responsibility |
| --- | --- |
| https://github.com/projectbluefin/utah | Image composition, extension pins, flavors, image faults; default issue destination |
| https://github.com/projectbluefin/utah-packages | Package factory, RPM builds and package patches |
| https://github.com/projectbluefin/common | Shared desktop defaults, setup hooks and Bluefin guidance |
| https://github.com/projectbluefin/bluefin | Mirrored Bluefin package contract |
| https://github.com/ublue-os/brew | Homebrew image layer and bootstrap (read-only for agents) |
| https://gitlab.com/redhat/hummingbird/containers | Hummingbird base distribution; follow its contribution and reporting guidance |
| https://github.com/projectbluefin/documentation | Website rendering and documentation wrapper |

Start uncertain image reports in Utah, using `BUG_REPORT_URL` from os-release.
For a permanent fix, clone Utah into a writable home/workspace, check out the
image commit, initialize submodules and read that checkout's AGENTS.md and
skill router. `git ls-tree COMMIT system_files/shared/usr/share/gnome-shell/extensions/UUID`
shows a vendored extension's Git commit; `gitmodules` maps its source URL.
Build commands and CI requirements belong to that checkout, not this bundle.
