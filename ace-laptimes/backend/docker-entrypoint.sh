#!/bin/sh
# Runs as root only long enough to make the data volume writable by the
# application user, then drops privileges for the actual process.
#
# The chown matters on upgrade: a volume created by an earlier image, when
# this container ran everything as root, still holds root-owned files. A
# fresh volume inherits ownership from the image and needs nothing, but
# handling both here means an upgrade does not require a manual chown.
set -e

if [ "$(id -u)" = "0" ]; then
    chown -R app:app /app/data 2>/dev/null || \
        echo "warning: could not take ownership of /app/data; check the volume's permissions" >&2
    exec gosu app "$@"
fi

# Already unprivileged (someone passed --user); nothing to drop.
exec "$@"
