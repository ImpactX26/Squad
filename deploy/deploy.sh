#!/usr/bin/env bash
# Deploy main on the server (ARCHITECTURE.md §17.4). Run as the deploy user, from anywhere:
#   ~/Squad/deploy/deploy.sh
# It stops at the first failure. The services are restarted only after everything built.
set -euo pipefail

cd "$(dirname "$0")/.."

die() { printf 'deploy: %s\n' "$*" >&2; exit 1; }
step() { printf '\n==> %s\n' "$*"; }

[ "$(id -u)" -ne 0 ] || die "run this as the deploy user, not root: root-owned files would break the services"

# The hostname is deploy/Caddyfile's site address: its first line that isn't blank or a comment.
host=$(awk '!/^[[:space:]]*(#|$)/ { print $1; exit }' deploy/Caddyfile)
[ -n "$host" ] && [ "$host" != "servicemesh.invalid" ] \
  || die "set the server's hostname in deploy/Caddyfile first (it still says ${host:-nothing})"
[ -f backend/.env ] || die "backend/.env is missing: write it by hand on the server (§17.3)"
[ -f web/.env.local ] || die "web/.env.local is missing: write it by hand on the server (§17.3)"
git diff --quiet HEAD -- || die "the checkout has local changes; the server only ever runs main"

step "git pull --ff-only"
git pull --ff-only

step "backend: uv sync --frozen"
(cd backend && uv sync --frozen)

step "web: pnpm install --frozen-lockfile && pnpm build"
(cd web && pnpm install --frozen-lockfile && pnpm build)

step "restart servicemesh-mcp, servicemesh-api, servicemesh-web"
sudo systemctl restart servicemesh-mcp servicemesh-api servicemesh-web

step "waiting for https://$host/api/health"
deadline=$((SECONDS + 60))
while [ "$SECONDS" -lt "$deadline" ]; do
  if curl -fsS --max-time 5 "https://$host/api/health" 2>/dev/null | grep -q '"status":"ok"'; then
    printf '\nLive: https://%s (%s)\n' "$host" "$(git log -1 --format='%h %s')"
    exit 0
  fi
  sleep 2
done

printf '\ndeploy: https://%s/api/health is not ok after 60 s. Last 50 lines of servicemesh-api:\n' "$host" >&2
journalctl -u servicemesh-api -n 50 --no-pager >&2 2>/dev/null || sudo journalctl -u servicemesh-api -n 50 --no-pager >&2
exit 1
