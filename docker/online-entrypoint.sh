#!/usr/bin/env sh
set -eu

if [ "${ARSENAL_INSTALL_DASHBOARD:-0}" = "1" ]; then
  python -m meme_arsenal.bootstrap
fi

exec "$@"
