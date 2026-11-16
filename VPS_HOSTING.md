# VPS hosting — same app, much lower cost than Railway usage billing

Run the **same Docker image** as Railway: billing UI, PostgreSQL, WhatsApp QR, PDF send, volume-backed `/app/data`. No feature changes — only fixed-price hosting.

Typical cost: **~€4–5/month** (e.g. Hetzner CX22/CX23 with 4 GB RAM) instead of **~$15+/month** Railway memory usage.

## Requirements

- A small Linux VPS (Ubuntu 24.04 recommended), **≥ 1 GB RAM** (4 GB comfortable for WhatsApp + Chromium)
- Docker Engine + Docker Compose plugin
- Optional: domain pointing to the server IP

## One-time setup on the server

```bash
sudo apt-get update && sudo apt-get install -y git docker.io docker-compose-v2
sudo usermod -aG docker "$USER"
# log out and back in so docker group applies
```

Clone your billing repo (same repo you deploy on Railway):

```bash
git clone https://github.com/YOUR_USER/Rinse-RiseBilling.git
cd Rinse-RiseBilling/laundry-billing   # or repo root if Dockerfile is at top
```

## Start (same stack as `docker-compose` always had + persistent app data)

```bash
docker compose up --build -d
```

Open `http://SERVER_IP:8080` (or put **Caddy/Nginx** in front for HTTPS on port 443).

Persistent data:

| Volume | Purpose |
|--------|---------|
| `appdata` → `/app/data` | WhatsApp session, invoices, SQLite fallback files |
| `pgdata` | PostgreSQL database |

## Production hardening (recommended)

1. Change Postgres password in `docker-compose.yml` (`POSTGRES_PASSWORD` and `DATABASE_URL` on `web`).
2. Open firewall: allow `22`, `80`/`443` (or only `8080` if no reverse proxy).
3. Scan WhatsApp QR **once** via header → WhatsApp pill (session lives in `appdata`).

## Health check

```bash
curl -s http://127.0.0.1:8080/api/health | jq
```

Expect `dbOk: true`, `whatsappAvailable: true` after the bridge finishes starting.

## Moving from Railway (no logic change)

1. On Railway: note your public URL; optional Postgres dump if you need to migrate rows.
2. Deploy VPS with `docker compose` as above; mount ensures new WhatsApp scan once.
3. Import Postgres data if needed (`pg_dump` / `pg_restore`); otherwise start fresh DB in `pgdata`.
4. Point staff to the new URL; **stop** the Railway web service so you are not double-billed.
5. Do **not** run `Start Billing.bat` WhatsApp at the same time as the VPS (one active session per account).

## Optional environment overrides

Set in `docker-compose.yml` under `web.environment` or an `.env` file:

| Variable | Default on VPS compose | Notes |
|----------|------------------------|--------|
| `GUNICORN_THREADS` | `2` | Lower RAM; increase to `8` only if you see slow concurrent API calls |
| `WHATSAPP_ENABLED` | `1` | Keep `1` for cloud WhatsApp |
| `WEB_CONCURRENCY` | `1` | Leave at 1 |

## Updates

```bash
git pull
docker compose up --build -d
```

WhatsApp session and Postgres data survive rebuilds via volumes.