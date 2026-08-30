#!/bin/sh
set -e

echo "===== Starting Tor ====="
tor --SocksPort 9050 --RunAsDaemon 0 &
sleep 5

echo "===== Starting Privoxy ====="
privoxy --no-daemon /etc/privoxy/config &
sleep 2

echo "===== Starting Cloudflare WARP daemon ====="
warp-svc &
sleep 5

echo "===== Registering WARP (anonymous, free) ====="
warp-cli --accept-tos registration new || echo "WARP already registered or registration failed - continuing"

echo "===== Setting WARP to proxy mode ====="
warp-cli --accept-tos mode proxy || echo "Could not set WARP proxy mode - continuing without WARP"

echo "===== Connecting WARP ====="
warp-cli --accept-tos connect || echo "Could not connect WARP - continuing without WARP"

echo "===== Waiting for WARP connection (up to 20s) ====="
i=0
while [ "$i" -lt 10 ]; do
  STATUS=$(warp-cli --accept-tos status 2>/dev/null | grep -i "Status update" || echo "unknown")
  echo "WARP status: $STATUS"
  case "$STATUS" in
    *Connected*) break ;;
  esac
  i=$((i + 1))
  sleep 2
done

echo "===== Starting bgutil PO token provider ====="
node /opt/bgutil-ytdlp-pot-provider/server/build/main.js &
sleep 2

export HTTP_PROXY=http://127.0.0.1:8118
export HTTPS_PROXY=http://127.0.0.1:8118

echo "===== Starting Flask app ====="
exec gunicorn \
    --bind 0.0.0.0:$PORT \
    --workers 1 \
    --threads 4 \
    --timeout 600 \
    app:app
