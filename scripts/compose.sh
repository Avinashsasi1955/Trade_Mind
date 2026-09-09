#!/bin/sh
set -eu

if docker compose version >/dev/null 2>&1; then
  exec docker compose -f docker-compose.v3.yml "$@"
fi
if command -v docker-compose >/dev/null 2>&1; then
  exec docker-compose -f docker-compose.v3.yml "$@"
fi
if [ -x /opt/homebrew/lib/docker/cli-plugins/docker-compose ]; then
  exec /opt/homebrew/lib/docker/cli-plugins/docker-compose -f docker-compose.v3.yml "$@"
fi
echo "Docker Compose v2 is required." >&2
exit 1
