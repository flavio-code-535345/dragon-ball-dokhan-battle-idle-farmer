#!/bin/sh
# One-shot helper of the all-in-one stack (docker-compose.redroid.yml).
#
# redroid (Android in a container) needs the "binder" driver of the HOST kernel.
# This loads it and, when the host's /etc/modules-load.d and /etc/modprobe.d are
# mounted at /host-etc/..., makes the host load it again after every reboot.
# Needs a privileged container with the host's /lib/modules mounted read-only.
set -u

# Overridable for the tests only.
PROC="${HOST_SETUP_PROC:-/proc}"
DEV="${HOST_SETUP_DEV:-/dev}"
MODULES="${HOST_SETUP_MODULES:-/lib/modules}"
HOST_ETC="${HOST_SETUP_ETC:-/host-etc}"
KERNEL="${HOST_SETUP_KERNEL:-$(uname -r)}"

MODULE=binder_linux
OPTIONS=devices=binder,hwbinder,vndbinder

log() { echo "host-setup: $*"; }

binder_ready() {
  # binderfs (Android mounts its own instance) or the classic /dev/binder device
  grep -qE '[[:space:]]binder$' "$PROC/filesystems" 2>/dev/null || [ -c "$DEV/binder" ]
}

# Docker inside an unprivileged LXC container (or rootless Docker) runs in a user
# namespace: root there is not root on the host, and Android cannot start.
in_user_namespace() {
  awk 'NR == 1 { exit !($1 != 0 || $2 != 0) }' "$PROC/self/uid_map" 2>/dev/null
}

use_emulator() {
  cat <<EOF
host-setup: Use the emulator stack instead (docker-compose.emulator.yml): Google's Android
host-setup: emulator in a container, which only needs /dev/kvm. On Proxmox, give the Docker
host-setup: container KVM on the Proxmox host first (102 = its ID):
host-setup:   pct set 102 -dev0 /dev/kvm,mode=0666 && pct reboot 102
host-setup: Details: docs/DEVICE_SETUP.md, option C.
EOF
}

persist() {
  header='# Written by dokkan-idle-farmer host-setup for redroid. Delete to undo.'
  if [ -d "$HOST_ETC/modprobe.d" ]; then
    printf '%s\noptions %s %s\n' "$header" "$MODULE" "$OPTIONS" \
      > "$HOST_ETC/modprobe.d/redroid.conf"
  fi
  if [ -d "$HOST_ETC/modules-load.d" ]; then
    printf '%s\n%s\n' "$header" "$MODULE" > "$HOST_ETC/modules-load.d/redroid.conf" &&
      log "the host loads $MODULE at boot (/etc/modules-load.d/redroid.conf)"
  fi
}

if in_user_namespace; then
  log "ERROR: Docker runs inside an unprivileged LXC container (or rootless Docker)."
  log "Android in a container (redroid) cannot start there, binder or not."
  use_emulator
  exit 1
fi

if binder_ready; then
  log "binder is available in kernel $KERNEL"
  # Built-in drivers need nothing at boot; a loaded module does.
  grep -q "^$MODULE " "$PROC/modules" 2>/dev/null && persist
  exit 0
fi

log "loading $MODULE into kernel $KERNEL"
if [ ! -d "$MODULES/$KERNEL" ]; then
  log "WARNING: /lib/modules/$KERNEL is missing: mount the host's /lib/modules (read-only)"
fi
modprobe "$MODULE" "$OPTIONS" 2>&1 | sed 's/^/host-setup: modprobe: /'

if binder_ready; then
  log "binder is ready"
  persist
  exit 0
fi

case "$KERNEL" in
  *-pve)
    cat <<EOF
host-setup: ERROR: could not load binder into kernel $KERNEL. This is Proxmox, so Docker
host-setup: probably runs in an LXC container, which cannot load drivers.
EOF
    use_emulator
    ;;
  *)
    cat <<EOF
host-setup: ERROR: could not load binder into kernel $KERNEL, so Android (redroid)
host-setup: cannot start. Make binder available on the server, then redeploy the stack:
host-setup:   Ubuntu:   sudo apt install linux-modules-extra-\$(uname -r)
host-setup:   Any distro: the kernel needs CONFIG_ANDROID_BINDER_IPC (module binder_linux,
host-setup:             e.g. Arch: the linux-zen kernel). After a kernel update, reboot so the
host-setup:             running kernel matches /lib/modules.
host-setup:   Docker Desktop, Synology, Unraid: their kernels usually lack binder; use a
host-setup:             phone or an emulator instead.
EOF
    use_emulator
    ;;
esac
exit 1
