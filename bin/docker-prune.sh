#!/bin/bash
# docker-prune.sh - the box's weekly prune of docker images and build cache, which keeps the forge's job images.
#
# The opi's disk fills with images from deploys and CI. This removes every image no container uses that is a week old,
# and the build cache of the same age. A job image carries the label `house.job-image` and is kept: a job uses its image
# only while it runs, so on 2026-10-04 the prune took roost-swift-ci:6.3.2-noble and roost-ci:arm64, and every job that
# names one failed at the pull with "pull access denied". The images are in no registry, so a lost one is a rebuild
# from ci/images.
#
#   docker-prune.sh            prune, and log the disk after
#   docker-prune.sh --dry-run  list the job images that are kept and the images a prune would consider, and remove nothing
set -u
if [ "${1:-}" = "--dry-run" ]; then
  echo "kept, labelled house.job-image:"
  docker images --filter label=house.job-image --format '  {{.Repository}}:{{.Tag}}  {{.CreatedSince}}  {{.Size}}'
  echo "considered, a week old and unlabelled (those a container uses stay):"
  # `docker images` has no filter by age, so each image's own creation time is read and compared.
  limit=$(( $(date +%s) - 168 * 3600 ))
  docker images --format '{{.ID}} {{.Repository}}:{{.Tag}}' | sort -u | while read -r id name; do
    [ -z "$(docker image inspect "$id" --format '{{index .Config.Labels "house.job-image"}}' 2>/dev/null)" ] || continue
    created=$(date -d "$(docker image inspect "$id" --format '{{.Created}}' 2>/dev/null)" +%s 2>/dev/null) || continue
    [ "$created" -lt "$limit" ] && echo "  $name  $(( ($(date +%s) - created) / 86400 )) days old"
  done
  exit 0
fi
docker image prune -a -f --filter until=168h --filter 'label!=house.job-image'
docker builder prune -f --filter until=168h
df -h / /mnt/nvme 2>/dev/null | logger -t docker-prune
