# Keep Common's default command and full recipe set. Shallower imports win
# duplicate resolution, so Utah's overrides take precedence over Common's.

_utah-default: _default

import "/usr/share/ublue-os/just/00-common.just"
import "/usr/share/ublue-os/just/60-custom.just"
