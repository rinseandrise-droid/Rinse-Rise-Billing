#!/bin/sh
set -e

PORT="${PORT:-8080}"
BRIDGE_PUBLIC="${WHATSAPP_BRIDGE_PORT:-3001}"
BRIDGE_INTERNAL="${WHATSAPP_BRIDGE_INTERNAL_PORT:-3002}"
DATA="${DATA_DIR:-/app/data}"
WA_AUTH="${WHATSAPP_AUTH_DIR:-$DATA/whatsapp-auth}"
WA_CACHE="${WHATSAPP_CACHE_DIR:-$DATA/whatsapp-cache}"

mkdir -p "$DATA/invoices" "$WA_AUTH" "$WA_CACHE"
touch "$DATA/.persistent_volume" 2>/dev/null || true

# Seed bundled WA Web HTML when volume cache is empty (QR appears faster)
WA_VER="${WHATSAPP_WEB_VERSION:-2.3000.1043441279-alpha}"
if [ -f "/app/whatsapp-bridge/wa-cache/${WA_VER}.html" ] && [ ! -f "$WA_CACHE/${WA_VER}.html" ]; then
  cp "/app/whatsapp-bridge/wa-cache/${WA_VER}.html" "$WA_CACHE/${WA_VER}.html"
  echo "Seeded WhatsApp Web cache (${WA_VER})"
fi

# Stale lock from a previous container must not block startup
rm -f "$WA_AUTH/.bridge.lock" 2>/dev/null || true

export NODE_OPTIONS="${NODE_OPTIONS:---max-old-space-size=1024}"
export RAILWAY_ENVIRONMENT="${RAILWAY_ENVIRONMENT:-1}"
export WHATSAPP_CLIENT_ID="${WHATSAPP_CLIENT_ID:-rinse-rise}"
export WHATSAPP_AUTH_DIR="$WA_AUTH"
export WHATSAPP_CACHE_DIR="$WA_CACHE"
export WHATSAPP_BRIDGE_PORT="$BRIDGE_PUBLIC"
export WHATSAPP_BRIDGE_INTERNAL_PORT="$BRIDGE_INTERNAL"
export WHATSAPP_BRIDGE_URL="http://127.0.0.1:${BRIDGE_PUBLIC}"
export DATA_DIR="$DATA"
export PUPPETEER_EXECUTABLE_PATH="${PUPPETEER_EXECUTABLE_PATH:-/usr/bin/chromium}"

bridge_internal_healthy() {
  curl -fsS "http://127.0.0.1:${BRIDGE_INTERNAL}/health" >/dev/null 2>&1
}

bridge_public_healthy() {
  curl -fsS "http://127.0.0.1:${BRIDGE_PUBLIC}/health" >/dev/null 2>&1
}

start_proxy_background() {
  if [ "${WHATSAPP_ENABLED:-1}" = "0" ]; then
    return 0
  fi
  echo "Starting WhatsApp health proxy on port ${BRIDGE_PUBLIC}..."
  (
    cd /app/whatsapp-bridge
    while true; do
      node proxy.js >> "$DATA/whatsapp-proxy.log" 2>&1
      echo "WhatsApp proxy exited — restarting in 3s..."
      sleep 3
    done
  ) &
}

start_bridge_background() {
  if [ "${WHATSAPP_ENABLED:-1}" = "0" ]; then
    echo "WhatsApp bridge disabled (set WHATSAPP_ENABLED=1 on Railway to enable QR scanner)"
    return 0
  fi

  (
    cd /app/whatsapp-bridge
    backoff=5
    while true; do
      if [ -f "$WA_AUTH/.bridge-restart-requested" ]; then
        rm -f "$WA_AUTH/.bridge-restart-requested" "$WA_AUTH/.bridge.lock" 2>/dev/null || true
        pkill -f "node server.js" 2>/dev/null || true
        sleep 2
      fi

      if bridge_internal_healthy; then
        sleep 15
        continue
      fi

      if [ -f "$WA_AUTH/.bridge.lock" ]; then
        pid="$(cat "$WA_AUTH/.bridge.lock" 2>/dev/null || echo "")"
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
          echo "Bridge pid $pid exists but /health failed — clearing stale lock"
        fi
        rm -f "$WA_AUTH/.bridge.lock"
      fi

      echo "Starting WhatsApp bridge on port ${BRIDGE_INTERNAL} (auth: $WA_AUTH)..."
      WHATSAPP_BRIDGE_PORT="$BRIDGE_INTERNAL" node server.js >> "$DATA/whatsapp-bridge.log" 2>&1
      echo "WhatsApp bridge exited — restarting in ${backoff}s..."
      sleep "$backoff"
      if [ "$backoff" -lt 60 ]; then
        backoff=$((backoff + 5))
      fi
    done
  ) &
}

start_proxy_background
start_bridge_background

if [ "${WHATSAPP_ENABLED:-1}" != "0" ]; then
  echo "WhatsApp proxy + bridge starting in background..."
  waited=0
  while [ "$waited" -lt 20 ]; do
    if bridge_public_healthy; then
      echo "WhatsApp proxy is up (scanner may still be loading)."
      break
    fi
    sleep 1
    waited=$((waited + 1))
  done
fi

if [ ! -f "$DATA/.persistent_volume" ]; then
  echo "WARNING: Railway Volume may not be mounted at /app/data — WhatsApp will need QR scan after every deploy."
fi

echo "Starting Gunicorn on port ${PORT}..."
echo "Persistent data directory: $DATA (mount a Railway Volume here)"
exec gunicorn \
  --bind "0.0.0.0:${PORT}" \
  --workers "${WEB_CONCURRENCY:-1}" \
  --threads 8 \
  --timeout 120 \
  --access-logfile - \
  --error-logfile - \
  --chdir /app/server \
  app:app
