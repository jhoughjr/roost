# Restore the opi stack

This file tells you how to rebuild the opi from the backups on this drive.
The backup script is `~/bin/opi-backup.sh` on the opi, and cron runs it at 03:30 every night.

## The repositories

Two restic repositories live in `restic/` on this drive.

- `opi-critical`: the PostgreSQL dumps, the dokku tree, `/etc`, the Home Assistant config, the docker volumes, and the host metadata. About 1.5 GB.
- `opi-bulk`: the home directory of `jimmy`, without the caches, the toolchains, and the runner checkouts. About 1.7 GB.

## The password

You cannot read either repository without the password.
Two copies exist, and both are outside the repositories:

- On the opi: `~/.config/opi-backup/password`
- On the mini: `~/.config/opi-backup/restic-password`

Put a third copy in a password manager.
If the opi and the mini both fail, the copies on them are gone, and the backups are unreadable.

## Read the backups

Run these commands from any machine that has restic and can reach the drive.

```
export RESTIC_PASSWORD_FILE=~/.config/opi-backup/restic-password
export R="/Users/jimmyhoughjr/backupdrive/restic/opi-critical"
restic -r "$R" snapshots
restic -r "$R" ls latest
```

Use `sftp:jimmyhoughjr@jimmys-mac-mini.local:/Users/jimmyhoughjr/backupdrive/restic/opi-critical` for the path when you work from a different machine.

## Restore order after a total loss of the opi

1. Install Ubuntu on the new hardware, and make the user `jimmy` with uid 1000.
2. Restore the metadata, and read it before you continue.

```
restic -r "$R" restore latest --target /restore --include '*/meta'
```

`dpkg-selections.txt` lists the packages, `snap-list.txt` lists the snaps, and `docker-ps.txt` lists every container with its image.

3. Install docker and dokku, then install the packages from `dpkg-selections.txt`.
4. Restore the dokku tree. It carries the Forgejo repositories, the vault data, the app configs, the domains, and the TLS certificates.

```
restic -r "$R" dump latest '*/tar/dokku.tar' | sudo tar -xf - -C /var/lib/dokku
```

5. Restore `/etc` selectively. Do not overwrite the whole directory on a new install, because the hardware differs.

```
restic -r "$R" dump latest '*/tar/etc.tar' | tar -xf - -C /tmp/etc-restore
```

Copy the nginx config, the systemd unit files, and the cloudflared config out of `/tmp/etc-restore`.

6. Restore the Home Assistant config.

```
restic -r "$R" dump latest '*/tar/homeassistant.tar' | tar -xf - -C ~/homeassistant
```

7. Start the PostgreSQL containers, then load the dumps. The dump is a full cluster dump, and it makes the databases, the roles, and the grants.

```
restic -r "$R" dump latest '*/pg/mwstack-pg-staging.sql' | docker exec -i mwstack-pg-staging psql -U postgres
```

8. Restore the docker volumes that hold data. The `vol-` files hold one volume each.

```
restic -r "$R" dump latest '*/tar/vol-act_runner_data.tar' | docker run --rm -i -v act_runner_data:/dst alpine tar -xf - -C /dst
```

9. Restore the home directory from `opi-bulk`.
10. Register the GitHub Actions runners again. The tokens do not survive a restore.
11. Rebuild the docker images from their Dockerfiles. We do not back the images up, because they rebuild.

## Verify a backup

`restic check` runs against `opi-critical` on every nightly run.
Run this command to also verify the stored data of the bulk repository:

```
restic -r "$R" check --read-data-subset 5%
```

## What this backup does not cover

- The drive sits in the same building as the opi and the mini. A fire takes all three.
  Send `opi-critical` offsite, because it is small.
- The docker images and the build caches. They rebuild.
- The GitHub Actions runner tokens. Register the runners again.
