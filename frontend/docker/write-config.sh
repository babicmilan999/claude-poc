#!/bin/sh
# Runs from the nginx image's /docker-entrypoint.d/ at container start. Writes
# config.json for real-API builds from environment variables (all optional):
#   API_BASE_URL      agent service base URL as the browser sees it (e.g. http://localhost:8000)
#   API_MODE          http or mock; real (the build arg's name) is written as http
#   CUSTOMER_APP_URL  ops console only: the customer app, for watch-mode links
# With none set, no file is written and the build's environment.real.ts applies.
set -eu
out=/usr/share/nginx/html/config.json
rm -f "$out"
[ -n "${API_BASE_URL:-}${API_MODE:-}${CUSTOMER_APP_URL:-}" ] || exit 0

json_string() { printf '"%s"' "$(printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g')"; }

fields=""
add() { fields="${fields:+$fields,}\"$1\":$(json_string "$2")"; }
[ -n "${API_BASE_URL:-}" ] && add apiBaseUrl "$API_BASE_URL"
if [ -n "${API_MODE:-}" ]; then
  case "$API_MODE" in
    http|real) add apiMode http ;;
    mock) add apiMode mock ;;
    *) echo "write-config: WARNING: ignoring unknown API_MODE '$API_MODE' (use real, http or mock)" >&2 ;;
  esac
fi
[ -n "${CUSTOMER_APP_URL:-}" ] && add customerAppUrl "$CUSTOMER_APP_URL"
printf '{%s}\n' "$fields" > "$out"
echo "write-config: $out = $(cat "$out")"
