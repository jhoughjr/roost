#!/usr/bin/env bash
# backup-watch.sh - say so when the nightly backup did not finish.
#
# This runs ON the opi, a few hours after the backup window, and it is deliberately
# not part of opi-backup.sh. The way that script fails is that the box stops: it
# hard-reset mid-write on 2026-09-07 and again on 2026-09-09, both at 03:31. A trap,
# a final line, an exit code, any reporting inside the run dies with the process. Only
# something that runs later can tell you the run never came back.
#
# What it reads is the stamp the backup writes when it reaches the end, so a run that
# died leaves the stamp at yesterday's date and this notices. It asks nothing of restic
# and needs no repository password, which keeps this cheap enough to run often.
#
# It found the estate had gone six days on every-other-night backups with nothing said.
set -euo pipefail

STATE="${HOME}/.local/state/opi-backup"
STAMP="${STATE}/last-success"
SEEN="${STATE}/watch-told"
# 26 hours: a nightly job plus room for a long run, so a slow night is not an alarm.
STALE_HOURS="${OPI_BACKUP_STALE_HOURS:-26}"

TOPIC="${ROOST_NTFY_TOPIC:-$(grep "^ROOST_NTFY_TOPIC=" "${HOME}/.roostrc" 2>/dev/null | cut -d= -f2- || true)}"
notify() {
    local title="$1" msg="$2" prio="${3:-default}"
    if [ -z "${TOPIC}" ]; then
        echo "no ROOST_NTFY_TOPIC; would have sent: ${title} - ${msg}"
        return 0
    fi
    curl -s -m 10 -H "Title: ${title}" -H "Priority: ${prio}" \
        -d "${msg}" "https://ntfy.sh/${TOPIC}" >/dev/null 2>&1 || true
}

now="$(date +%s)"
if [ -f "${STAMP}" ]; then
    last="$(date -d "$(cat "${STAMP}")" +%s 2>/dev/null || echo 0)"
else
    last=0
fi
age_h=$(( (now - last) / 3600 ))

if [ "${last}" -eq 0 ]; then
    state="the backup has never recorded a finish"
elif [ "${age_h}" -ge "${STALE_HOURS}" ]; then
    state="the last backup that finished was ${age_h} hours ago"
else
    # Healthy. Say so once, if the last thing said was bad news.
    if [ -f "${SEEN}" ]; then
        notify "backup: finished again" "$(hostname -s): a backup completed ${age_h}h ago, after $(cat "${SEEN}")."
        rm -f "${SEEN}"
    fi
    echo "backup-watch: last finish ${age_h}h ago, inside the ${STALE_HOURS}h window"
    exit 0
fi

echo "backup-watch: ${state}"

# Said once a day while it stays broken. A message every pass would be the same
# silence by a louder route, and this is meant to be readable at a glance.
told=0
[ -f "${SEEN}" ] && told="$(stat -c %Y "${SEEN}" 2>/dev/null || echo 0)"
if [ $(( now - told )) -ge 86400 ]; then
    # The reason, when the box can give one. An unclean boot in the window is the
    # difference between "the backup is broken" and "the box died again".
    why=""
    if command -v journalctl >/dev/null 2>&1; then
        reset_at="$(journalctl --list-boots --no-pager 2>/dev/null | tail -2 | head -1 | awk '{print $(NF-2), $(NF-1)}')"
        [ -n "${reset_at}" ] && why=" The previous boot ended ${reset_at}, so check for an unclean reset."
    fi
    # "in 496934 hours" is what an epoch-zero stamp reads as, and a number that
    # absurd makes a real alert look like a bug in the alert.
    span="in ${age_h}h"
    [ "${last}" -eq 0 ] && span="ever"
    notify "backup: no finish ${span}" \
        "$(hostname -s): ${state}. Nothing is being written to the restic repositories, so a restore would land on stale data.${why}" \
        high
    printf '%s\n' "${state}" > "${SEEN}"
fi
