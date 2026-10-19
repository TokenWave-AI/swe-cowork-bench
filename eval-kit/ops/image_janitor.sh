#!/bin/bash
# Keep Docker disk usage bounded: when free space on / drops below LOW_GIB,
# remove release task images that no container (running or stopped) uses,
# oldest first, until free space reaches HIGH_GIB. Images are digest-pinned
# and re-pulled on demand by the runtime's ensure_image().
LOW_GIB=${LOW_GIB:-18}
HIGH_GIB=${HIGH_GIB:-28}
LOG=/var/log/image-janitor.log
free_gib() { df -BG --output=avail / | tail -1 | tr -dc 0-9; }
SEEN=/var/lib/image-janitor.seen; touch $SEEN
while true; do
  # record first time each image id is observed (pull time proxy)
  for id in $(docker images --no-trunc -q | sort -u); do grep -q "^$id " $SEEN || echo "$id $(date +%s)" >> $SEEN; done
  if [ "$(free_gib)" -lt "$LOW_GIB" ]; then
    used=$(docker ps -a --format '{{.Image}}' | xargs -r -n1 docker image inspect -f '{{.Id}}' 2>/dev/null | sort -u)
    docker images --no-trunc --format '{{.CreatedAt}}|{{.ID}}|{{.Repository}}' \
      | grep '|harbor.tokenwave.us/release/' | sort | while IFS='|' read -r _ id repo; do
        [ "$(free_gib)" -ge "$HIGH_GIB" ] && break
        echo "$used" | grep -q "$id" && continue
        seen=$(grep "^$id " $SEEN | cut -d' ' -f2)
        [ -z "$seen" ] || [ $(( $(date +%s) - seen )) -lt 2700 ] && continue
        if docker rmi -f "$id" >/dev/null 2>&1; then
          echo "$(date -u +%FT%TZ) removed $repo $id free=$(free_gib)G" >> $LOG
        fi
      done
    docker builder prune -f >/dev/null 2>&1
  fi
  sleep 60
done
