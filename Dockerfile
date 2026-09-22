# Rinse & Rise Laundry Billing — web app + WhatsApp QR bridge (hosted)
FROM node:20-bookworm-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080 \
    WHATSAPP_BRIDGE_URL=http://127.0.0.1:3001 \
    WHATSAPP_BRIDGE_PORT=3001 \
    WHATSAPP_BRIDGE_INTERNAL_PORT=3002 \
    PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium \
    PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=true \
    DATA_DIR=/app/data \
    WHATSAPP_AUTH_DIR=/app/data/whatsapp-auth \
    WHATSAPP_CACHE_DIR=/app/data/whatsapp-cache \
    WHATSAPP_ENABLED=1 \
    WHATSAPP_REMOTE_CACHE=1

WORKDIR /app

# Python (Flask API) + Chromium (WhatsApp Web via Puppeteer)
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3 \
        python3-pip \
        curl \
        fontconfig \
        fonts-dejavu-core \
        fonts-liberation \
        ca-certificates \
        libnss3 \
        libnspr4 \
        libatk1.0-0 \
        libatk-bridge2.0-0 \
        libcups2 \
        libdrm2 \
        libdbus-1-3 \
        libglib2.0-0 \
        libgtk-3-0 \
        libxkbcommon0 \
        libxcomposite1 \
        libxdamage1 \
        libxfixes3 \
        libxrandr2 \
        libxrender1 \
        libxss1 \
        libgbm1 \
        libasound2 \
        libpango-1.0-0 \
        libpangocairo-1.0-0 \
        libcairo2 \
        libx11-6 \
        libx11-xcb1 \
        libxcb1 \
        libxext6 \
        libxi6 \
        libxtst6 \
        chromium \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt --break-system-packages

COPY whatsapp-bridge/package.json whatsapp-bridge/package-lock.json ./whatsapp-bridge/
RUN cd whatsapp-bridge && npm ci --omit=dev --ignore-scripts \
    && node -e "const fs=require('fs'); const c='/usr/bin/chromium'; if(!fs.existsSync(c)) { console.error('Chromium missing:', c); process.exit(1); } console.log('Chromium OK:', c);"

COPY whatsapp-bridge/patch-wwebjs.js ./whatsapp-bridge/
RUN cd whatsapp-bridge && node patch-wwebjs.js

COPY . .

RUN mkdir -p /app/data/invoices /app/data/whatsapp-auth /app/data/whatsapp-cache \
    && cp whatsapp-bridge/wa-cache/*.html /app/data/whatsapp-cache/ 2>/dev/null || true \
    && sed -i 's/\r$//' docker-entrypoint.sh \
    && chmod +x docker-entrypoint.sh

EXPOSE 8080

HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=6 \
    CMD curl -fsS "http://127.0.0.1:${PORT}/api/live" || exit 1

ENTRYPOINT ["/app/docker-entrypoint.sh"]
