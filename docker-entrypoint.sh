#!/bin/sh
# Container entrypoint: run MedServ as a non-root user that matches the host
# bind-mount owner.
#
# The container starts as root (see Dockerfile), aligns the `medserv` user and
# group with PUID/PGID, makes sure the data directory is writable, then drops
# privileges with gosu. This lets a NAS bind mount (e.g. /volume1/docker/medserv)
# owned by any user/group work without manual chown.
set -eu

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

if [ "$(id -g medserv 2>/dev/null || echo)" != "$PGID" ]; then
    groupmod -o -g "$PGID" medserv 2>/dev/null || true
fi
if [ "$(id -u medserv 2>/dev/null || echo)" != "$PUID" ]; then
    usermod -o -u "$PUID" medserv 2>/dev/null || true
fi

DATA_DIR="${MEDSERV_DATA_DIR:-/data}"
mkdir -p "$DATA_DIR"
chown -R medserv:medserv "$DATA_DIR" 2>/dev/null || true

if [ "$(id -u)" = "0" ]; then
    exec gosu medserv "$@"
fi
exec "$@"
