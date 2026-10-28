#!/usr/bin/env bash
# Pulls the Ollama models used by a LiteLLM profile (`make models`).
#
#   infra/scripts/pull-models.sh [profile]
#
# Uses the native `ollama` CLI if it is installed (the default setup); otherwise
# the `ollama` container from the Compose profile, if it is running.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# Profile: argument, then $LLM_PROFILE, then LLM_PROFILE in .env, then `ollama`.
PROFILE="${1:-${LLM_PROFILE:-}}"
if [ -z "$PROFILE" ] && [ -f "$ROOT/.env" ]; then
  PROFILE="$(sed -n -E 's/^LLM_PROFILE=([^[:space:]#]*).*/\1/p' "$ROOT/.env" | tail -n 1)"
fi
PROFILE="${PROFILE:-ollama}"
CONFIG="$ROOT/infra/litellm/$PROFILE.yaml"

if [ ! -f "$CONFIG" ]; then
  echo "models: unknown LLM profile '$PROFILE' (no $CONFIG)" >&2
  exit 1
fi

# `model: ollama_chat/<name:tag>` or `model: ollama/<name:tag>` lines, de-duplicated.
MODELS="$(sed -n -E 's#^[[:space:]]*model:[[:space:]]*ollama(_chat)?/([^[:space:]#]+).*#\2#p' "$CONFIG" | sort -u)"

if [ -z "$MODELS" ]; then
  echo "models: profile '$PROFILE' uses no Ollama models, nothing to pull"
  exit 0
fi

if command -v ollama >/dev/null 2>&1; then
  PULL=(ollama pull)
elif command -v docker >/dev/null 2>&1 \
  && docker compose -f "$ROOT/infra/docker-compose.yml" ps --status running --services 2>/dev/null | grep -qx ollama; then
  PULL=(docker compose -f "$ROOT/infra/docker-compose.yml" exec -T ollama ollama pull)
else
  echo "models: Ollama not found. Install it (https://ollama.com) and start it, or run" >&2
  echo "        docker compose -f infra/docker-compose.yml --env-file .env --profile ollama up -d ollama" >&2
  exit 1
fi

echo "models: pulling for profile '$PROFILE':"
echo "$MODELS" | sed 's/^/  - /'
while IFS= read -r model; do
  "${PULL[@]}" "$model"
done <<< "$MODELS"
