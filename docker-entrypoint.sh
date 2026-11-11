#!/bin/sh
set -eu

# Railway volumes may be mounted with root ownership even when the image
# directory was chowned at build time. Repair only the dedicated data volume,
# then permanently drop privileges before starting the application.
mkdir -p "${IMPERION_STORAGE_DIR:-/data}"
chown -R imperion:imperion "${IMPERION_STORAGE_DIR:-/data}"
exec gosu imperion "$@"
