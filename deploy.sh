#!/usr/bin/env bash
# Rebuild and restart StayFlow on the server, then wait until the app reports healthy.
# Run by GitHub Actions (see .github/workflows/deploy.yml) after `git pull`, or by hand:
#   cd ~/stayflow && git pull && ./deploy.sh
# Data in ./data (SQLite DBs, uploaded photos) and .env are never touched.
set -euo pipefail
cd "$(dirname "$0")"

echo "→ Deploying $(git log -1 --format='%h %s')"
docker compose up -d --build --remove-orphans
docker image prune -f >/dev/null  # keep the 40 GB disk from filling with old builds

app=$(docker compose ps -q app)
for _ in $(seq 1 45); do
  status=$(docker inspect -f '{{.State.Health.Status}}' "$app" 2>/dev/null || echo unknown)
  if [ "$status" = healthy ]; then
    echo "✅ Live and healthy"
    exit 0
  fi
  sleep 2
done
echo "❌ App did not become healthy. Recent logs:"
docker compose logs --tail 60 app
exit 1
