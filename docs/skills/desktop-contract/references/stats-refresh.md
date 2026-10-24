## Community stats in fastfetch (#860)

`bluefin-stats-refresh.service` fetches the Bluefin user count and Bazaar's
weekly installs for the terminal banner. Two rules keep the values visible:

- The unit runs as the static `bluefin-stats` user
  (`sysusers.d/utah-bluefin-stats.conf`) with `DynamicUser=no`,
  `CacheDirectory=bluefin-stats` and `CacheDirectoryMode=0755`.
  `DynamicUser=yes` always places the cache under `/var/cache/private`
  (mode 0700), which user shells cannot traverse. The hardening
  `DynamicUser=yes` implied (`RemoveIPC=`, `PrivateTmp=`, `NoNewPrivileges=`,
  `RestrictSUIDSGID=`, `ProtectSystem=strict`) is spelled out instead.
- The directory name is deliberately not `bluefin`. Machines that ran the old
  unit have `/var/cache/private/bluefin`; reusing the name would trigger
  systemd's private-to-public rename, the same migration that fails with
  `EACCES` for fwupd-refresh on this image (#385). The orphaned directory
  holds two small text files and is left alone.
- Common's `fastfetch.jsonc` reads only the `/usr/share/ublue-os` placeholders
  that `configure-branding.sh` writes, and `/usr` is read-only at runtime.
  `configure-services.sh` rewrites both stat commands to
  `cat /var/cache/bluefin-stats/<file> 2>/dev/null || cat /usr/share/ublue-os/<file>`,
  guarded like the other Common patches (already patched is a no-op; an
  unrecognised upstream line fails the build). The contract's
  `file_contains` asserts the rewritten commands.

`StatsRefreshCacheTests` in `tests/test_desktop_contract.py` pins the unit,
the sysusers entry, the script's write path, and runs the fastfetch patch
twice against Common's lines to prove it is idempotent and fails closed.
