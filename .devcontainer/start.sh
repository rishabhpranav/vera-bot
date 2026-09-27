#!/usr/bin/env bash
# Start the bot and make port 8080 public on the Codespace's github.dev URL.
cd "$(dirname "$0")/.."
nohup uvicorn vera.app:app --host 0.0.0.0 --port 8080 --workers 1 > /tmp/vera.log 2>&1 &
for i in $(seq 1 30); do curl -sf localhost:8080/v1/healthz >/dev/null && break; sleep 1; done
if [ -n "$CODESPACE_NAME" ]; then
  gh codespace ports visibility 8080:public -c "$CODESPACE_NAME" >/tmp/port.log 2>&1 || true
  URL="https://${CODESPACE_NAME}-8080.${GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN:-app.github.dev}"
  echo "$URL" > PUBLIC_URL.txt
  echo "Vera bot live at: $URL"
fi
