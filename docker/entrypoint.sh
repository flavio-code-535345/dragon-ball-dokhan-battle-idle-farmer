#!/bin/sh
# Prepares /data, keeps the adb key in the volume (so a paired phone stays
# paired after updates) and drops root privileges (PUID/PGID, default 1000).
set -eu

DATA_DIR="${DATA_DIR:-/data}"
mkdir -p "$DATA_DIR/adb"

link_adb_keys() {
  # adb keeps its RSA key pair in ~/.android
  home="$1"
  mkdir -p "$home"
  if [ ! -L "$home/.android" ]; then
    rm -rf "$home/.android"
    ln -s "$DATA_DIR/adb" "$home/.android"
  fi
}

if [ "$(id -u)" = "0" ] && [ "${RUN_AS_ROOT:-false}" != "true" ]; then
  PUID="${PUID:-1000}"
  PGID="${PGID:-1000}"
  if [ "$(id -g farmer)" != "$PGID" ]; then
    groupmod -o -g "$PGID" farmer
  fi
  if [ "$(id -u farmer)" != "$PUID" ]; then
    usermod -o -u "$PUID" farmer
  fi
  link_adb_keys /home/farmer
  chown -R farmer:farmer "$DATA_DIR" /home/farmer
  export HOME=/home/farmer
  exec setpriv --reuid=farmer --regid=farmer --init-groups "$@"
fi

link_adb_keys "${HOME:-/root}"
exec "$@"
