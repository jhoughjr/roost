#!/usr/bin/env bash
# Move containerd's store off the eMMC and onto the NVMe.
#
# The box already sets docker's data-root to /mnt/nvme/docker, which looks like
# the move was done. It was not. Docker here runs the containerd snapshotter, so
# the image layers live under /var/lib/containerd, which data-root does not
# cover — that is how the eMMC reached 92% while the NVMe sat at 8%.
#
# A bind mount rather than containerd's own `root` setting, on purpose. Setting
# root means writing /etc/containerd/config.toml, and generating one from
# `containerd config default` replaces the packaged defaults with whatever this
# containerd version thinks they should be. The bind mount changes where the
# bytes are and nothing else.
#
# Run with sudo. Every service on the box is down for the copy.
set -euo pipefail

SRC=/var/lib/containerd
DEST=/mnt/nvme/containerd
KEPT=/var/lib/containerd.moved-aside

[ "$(id -u)" -eq 0 ] || { echo "run this with sudo" >&2; exit 1; }
mountpoint -q /mnt/nvme || { echo "/mnt/nvme is not mounted; nothing to move onto" >&2; exit 1; }
mountpoint -q "$SRC" && { echo "$SRC is already a mount point — already done" >&2; exit 0; }

need=$(du -sxm "$SRC" | cut -f1)
free=$(df -Pm /mnt/nvme | awk 'NR==2 {print $4}')
[ "$free" -gt "$((need + 5120))" ] || {
  echo "need ${need}MB plus headroom, /mnt/nvme has ${free}MB" >&2; exit 1
}
echo "moving ${need}MB from $SRC to $DEST"

# Stop the socket first, or systemd starts docker again the moment something
# touches it, and the copy runs against a store being written to.
systemctl stop docker.socket docker.service containerd.service
sleep 2

mkdir -p "$DEST"
# -H matters: the overlayfs snapshotter hardlinks heavily between layers, and a
# copy that breaks them arrives larger than the original and wrong.
rsync -aHAX --numeric-ids --info=progress2 "$SRC/" "$DEST/"

# Kept, not deleted. The copy is verified by containerd starting and docker
# listing what it listed before; until somebody has seen that, the original is
# the way back.
mv "$SRC" "$KEPT"
mkdir -p "$SRC"

grep -q "^$DEST $SRC " /etc/fstab || echo "$DEST $SRC none bind 0 0" >> /etc/fstab
mount "$SRC"
mountpoint -q "$SRC" || { echo "bind mount did not take" >&2; exit 1; }

systemctl start containerd.service docker.service docker.socket
sleep 5
docker info >/dev/null || { echo "docker did not come back" >&2; exit 1; }

echo
echo "done. containerd now lives on the nvme:"
df -h / /mnt/nvme | sed 's/^/  /'
echo
echo "check the estate, then reclaim the eMMC copy:"
echo "  docker ps --format '{{.Names}}' | sort"
echo "  sudo rm -rf $KEPT"
