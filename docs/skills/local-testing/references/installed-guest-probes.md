## Installed guest probes

The review VM must check statistics timer enablement after installation, then
create Common's countme opt-out marker and require the service condition to
skip execution. Remove that marker and restart the timer in a `finally` block
so the probe leaves the enabled-by-default installation intact.

Installed-guest file copies require the installed SSH port and test-user
password, using SCP's uppercase `-P`. Exercise that helper after merging
harness changes; a missing function can otherwise fail only after installation.
