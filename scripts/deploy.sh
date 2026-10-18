#!/usr/bin/env sh
# Run on the server: pull the latest code, rebuild on a freshly pulled base image
# (picks up Python and Debian security patches), restart, and show status.
set -eu
cd "$(dirname "$0")/.."
git pull --ff-only
docker compose build --pull
docker compose up -d
docker image prune -f >/dev/null   # remove the previous, now-unused image
sleep 5
docker compose ps
docker compose logs --tail 5
