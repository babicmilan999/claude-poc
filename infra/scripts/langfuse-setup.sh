#!/usr/bin/env bash
# Langfuse first-run setup. Idempotent: only fills values that are empty.
#
#   infra/scripts/langfuse-setup.sh           prepare .env (run before the first `make up`)
#   infra/scripts/langfuse-setup.sh --check   also wait for Langfuse and verify the API keys
#
# What it does:
#   1. Creates .env from .env.example if it does not exist.
#   2. Generates the project API keys (LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY), an
#      admin login and Langfuse's internal secrets, and writes them into .env.
#   3. On its first start, langfuse-web reads these through its LANGFUSE_INIT_* variables
#      and creates the org `claude-poc`, the project `payments-support` with exactly
#      these keys, and the admin user. No clicking through the UI.
#
# The keys only take effect on a fresh Langfuse database. If Langfuse already ran
# with other keys, either create keys in the UI (Project settings > API keys) and
# paste them into .env, or reset Langfuse's volumes (this deletes all traces):
#   docker compose -f infra/docker-compose.yml --env-file .env down
#   docker volume rm claude-poc_langfuse_postgres_data claude-poc_langfuse_clickhouse_data \
#     claude-poc_langfuse_clickhouse_logs claude-poc_langfuse_minio_data claude-poc_langfuse_redis_data
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENV_FILE="$ROOT/.env"
CHECK=false
[ "${1:-}" = "--check" ] && CHECK=true

hex() { head -c "$1" /dev/urandom | od -An -tx1 | tr -d ' \n'; }
uuid() { local h; h="$(hex 16)"; echo "${h:0:8}-${h:8:4}-${h:12:4}-${h:16:4}-${h:20:12}"; }

# Value of KEY in .env (last occurrence), empty if missing.
get() { sed -n -E "s/^$1=(.*)$/\\1/p" "$ENV_FILE" | tail -n 1; }

# Sets KEY only if it is missing or empty in .env.
fill() {
  local key="$1" value="$2"
  if [ -n "$(get "$key")" ]; then return; fi
  if grep -q -E "^$key=" "$ENV_FILE"; then
    local tmp; tmp="$(mktemp)"
    awk -v k="$key" -v v="$value" 'BEGIN{FS=OFS="="} $1==k && !done {print k "=" v; done=1; next} {print}' "$ENV_FILE" > "$tmp"
    cat "$tmp" > "$ENV_FILE" && rm -f "$tmp"
  else
    printf '%s=%s\n' "$key" "$value" >> "$ENV_FILE"
  fi
  echo "langfuse-setup: set $key"
}

if [ ! -f "$ENV_FILE" ]; then
  cp "$ROOT/.env.example" "$ENV_FILE"
  echo "langfuse-setup: created .env from .env.example"
fi

fill LANGFUSE_PUBLIC_KEY "pk-lf-$(uuid)"
fill LANGFUSE_SECRET_KEY "sk-lf-$(uuid)"
fill LANGFUSE_INIT_USER_EMAIL "admin@claude-poc.local"
fill LANGFUSE_INIT_USER_NAME "Admin"
fill LANGFUSE_INIT_USER_PASSWORD "$(hex 12)"
fill LANGFUSE_NEXTAUTH_SECRET "$(hex 32)"
fill LANGFUSE_SALT "$(hex 32)"
fill LANGFUSE_ENCRYPTION_KEY "$(hex 32)"

URL="$(get LANGFUSE_HOST)"; URL="${URL:-http://localhost:3000}"
echo "langfuse-setup: .env is ready. Langfuse UI: $URL"
echo "  login: $(get LANGFUSE_INIT_USER_EMAIL) / password: LANGFUSE_INIT_USER_PASSWORD in .env"

if $CHECK; then
  echo "langfuse-setup: waiting for $URL ..."
  for _ in $(seq 1 90); do
    if curl -fsS "$URL/api/public/health" >/dev/null 2>&1; then break; fi
    sleep 2
  done
  if ! curl -fsS "$URL/api/public/health" >/dev/null 2>&1; then
    echo "langfuse-setup: Langfuse is not reachable at $URL (is the stack up?)" >&2
    exit 1
  fi
  if curl -fsS -u "$(get LANGFUSE_PUBLIC_KEY):$(get LANGFUSE_SECRET_KEY)" "$URL/api/public/projects" >/dev/null; then
    echo "langfuse-setup: API keys accepted"
  else
    echo "langfuse-setup: Langfuse rejected the API keys in .env (see the reset note at the top of this script)" >&2
    exit 1
  fi
fi
