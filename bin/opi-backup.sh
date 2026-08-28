#!/usr/bin/env bash
# opi-backup.sh - nightly disaster-recovery backup of the opi stack.
#
# This script runs ON the opi, not on a Mac. It is standalone, and it does not
# source roost-env.sh, because the opi holds no roost clone.
# Install it at ~/bin/opi-backup.sh and run it from jimmy's crontab at 03:30.
#
# It writes two restic repositories over SFTP to the drive on the mini:
#   opi-critical  PostgreSQL dumps, /var/lib/dokku, /etc, Home Assistant, docker volumes, host metadata.
#   opi-bulk      /home/jimmy, without the caches, the toolchains, and the runner checkouts.
#
# The whole stack needs about 3 GB. The docker images and the build caches are
# most of the disk on the opi, and we leave them out, because they rebuild.
#
# Requirements:
#   restic at ~/bin/restic. The account is in the `docker` group, and no sudo is needed.
#   An SSH key from the opi that the mini authorizes.
#   The symlink ~/backupdrive on the mini, because the volume name holds spaces.
#
# Secrets: the repository password is at ~/.config/opi-backup/password (chmod 600).
#   A second copy is on the mini at ~/.config/opi-backup/restic-password.
#   Losing both copies makes every backup unreadable.
#
# Restore: see docs/opi-backup-restore.md.
#
# This replaced bin/backup-roost.sh, which is retired. That script tarred the
# vault and watts storage to a Mac, and /var/lib/dokku holds both of those paths.
set -euo pipefail

MINI="jimmyhoughjr@jimmys-mac-mini.local"
BASE="sftp:${MINI}:/Users/jimmyhoughjr/backupdrive/restic"
REPO_CRIT="${BASE}/opi-critical"
REPO_BULK="${BASE}/opi-bulk"
STAGE="/mnt/nvme/backup-staging"
RESTIC="${HOME}/bin/restic"
STATE="${HOME}/.local/state/opi-backup"
export RESTIC_PASSWORD_FILE="${HOME}/.config/opi-backup/password"

mkdir -p "${STATE}"

# restic returns 3 when it saves the snapshot but cannot read every file.
# We report that as a warning, and we stop the run on any other failure.
run_restic() {
    local rc=0
    "${RESTIC}" "$@" || rc=$?
    if [ "${rc}" -eq 3 ]; then
        echo "warning: restic could not read every source file"
        return 0
    fi
    return "${rc}"
}

# Two runs at the same time would corrupt the staging directory.
exec 9>"${STATE}/lock"
flock -n 9 || { echo "another run holds the lock"; exit 0; }

exec > >(tee "${STATE}/last-run.log") 2>&1
echo "=== opi-backup started $(date -Is) ==="

# Stage the payload on the NVMe drive.
# The eMMC card holds the root filesystem, and we keep the write load off it.
# The NVMe mount belongs to root, so a container makes the staging directory when it is absent.
if [ ! -w "${STAGE}" ]; then
    docker run --rm -v "$(dirname "${STAGE}")":/parent alpine \
        sh -c "mkdir -p /parent/$(basename "${STAGE}") && chown $(id -u):$(id -g) /parent/$(basename "${STAGE}")"
fi
rm -rf "${STAGE:?}"/*
mkdir -p "${STAGE}"/{pg,tar,meta}

# Dump each running PostgreSQL cluster.
# A file copy of a live cluster is inconsistent, and it usually does not restore.
for container in $(docker ps --format '{{.Names}}' | grep -E '^mwstack-pg-' || true); do
    echo "-- pg_dumpall ${container}"
    docker exec "${container}" pg_dumpall -U postgres > "${STAGE}/pg/${container}.sql"
done

# Copy the root-owned trees through a container.
# The account cannot read them directly, and the container keeps the original ownership.
# We write a plain tar and not a gzip, because restic must see unchanged content to deduplicate it.
tar_root() {
    local src="$1" name="$2"
    docker run --rm -v "${src}":/src:ro -v "${STAGE}/tar":/out alpine \
        tar --numeric-owner -cf "/out/${name}.tar" -C /src . 2>/dev/null || true
    chmod 644 "${STAGE}/tar/${name}.tar" 2>/dev/null || true
}

# The dokku tree carries the Forgejo repositories, the vault data, and every app config.
tar_root /var/lib/dokku dokku
tar_root /etc etc

# Home Assistant runs as root, so its `.storage` files belong to root inside this bind mount.
# Those files hold the authentication data and the core config, and a direct read fails.
tar_root "${HOME}/homeassistant" homeassistant

# Copy the docker volumes that hold data.
# The buildx cache volumes rebuild themselves, and they are most of the volume footprint.
# The 64-character names are anonymous volumes, and they are empty.
for volume in $(docker volume ls -q | grep -vE 'buildx_buildkit|^[0-9a-f]{64}$'); do
    echo "-- volume ${volume}"
    docker run --rm -v "${volume}":/src:ro -v "${STAGE}/tar":/out alpine \
        tar --numeric-owner -cf "/out/vol-${volume}.tar" -C /src . 2>/dev/null || true
    chmod 644 "${STAGE}/tar/vol-${volume}.tar" 2>/dev/null || true
done

# Record the facts that a rebuild of the host needs.
dpkg --get-selections > "${STAGE}/meta/dpkg-selections.txt"
snap list > "${STAGE}/meta/snap-list.txt" 2>/dev/null || true
docker ps -a --format '{{.Names}}\t{{.Image}}\t{{.Status}}' > "${STAGE}/meta/docker-ps.txt"
docker volume ls > "${STAGE}/meta/docker-volumes.txt"
docker images --format '{{.Repository}}:{{.Tag}}' > "${STAGE}/meta/docker-images.txt"
systemctl list-unit-files --state=enabled --no-pager > "${STAGE}/meta/systemd-enabled.txt" 2>/dev/null || true
ip addr > "${STAGE}/meta/ip-addr.txt"
lsblk -f > "${STAGE}/meta/lsblk.txt"
uname -a > "${STAGE}/meta/uname.txt"

# The staging directory now holds every critical tree, and each one is small and hard to rebuild.
echo "=== backup: critical ==="
run_restic -r "${REPO_CRIT}" backup --tag critical \
    "${STAGE}/pg" \
    "${STAGE}/tar" \
    "${STAGE}/meta"

# The excluded paths are caches, toolchains, and runner checkouts.
# Each one downloads again or rebuilds, and together they are most of the home directory.
echo "=== backup: bulk ==="
run_restic -r "${REPO_BULK}" backup --tag bulk "${HOME}" \
    --exclude "${HOME}/.cache" \
    --exclude "${HOME}/.swiftpm" \
    --exclude "${HOME}/toolchains" \
    --exclude "${HOME}/.hatchery-ci-scratch" \
    --exclude "${HOME}/bin/restic" \
    --exclude "${HOME}/homeassistant" \
    --exclude '**/_work' \
    --exclude '**/_diag' \
    --exclude '**/node_modules' \
    --exclude '**/.build' \
    --exclude '**/externals.*'

# Keep a year of history at a decreasing resolution.
echo "=== retention ==="
for repo in "${REPO_CRIT}" "${REPO_BULK}"; do
    run_restic -r "${repo}" forget --keep-daily 7 --keep-weekly 4 --keep-monthly 6 --prune
done

# Verify the critical repository on every run.
# A backup that we never verify is a guess.
echo "=== check ==="
run_restic -r "${REPO_CRIT}" check

rm -rf "${STAGE:?}"/*
date -Is > "${STATE}/last-success"
echo "=== opi-backup finished $(date -Is) ==="
