# Rinse & Rise — Laundry Billing

POS billing app for **Rinse & Rise Laundryrite** with SQLite storage, PDF invoices, WhatsApp send, bill history, expenditure tracking, and Excel export.

## Quick start (Windows)

1. Install [Python 3](https://www.python.org/downloads/) and [Node.js](https://nodejs.org/)
2. Double-click **`Start Billing.bat`**
3. Open [http://localhost:8080](http://localhost:8080)

## Features

- Customer billing with rate card search
- Bill history (Today / Week / Month / Year) with Excel download
- Order status: Pending → Order Ready → Delivery Done
- Regular customer ★ favorites by phone number
- Password-protected expenditure page
- Invoice PDF generation
- WhatsApp PDF send (scan QR once via header pill)

## Stack

- Frontend: HTML, CSS, JavaScript
- Backend: Flask + SQLite
- WhatsApp: Node.js `whatsapp-web.js` bridge (port 3001)

## Project structure

```
laundry-billing/
├── index.html          # Main UI
├── js/                 # Client logic
├── css/                # Styles
├── server/             # Flask API
├── data/               # schema.sql, rates.json (DB created on first run)
├── whatsapp-bridge/    # WhatsApp service
└── Start Billing.bat   # One-click launcher
```

## Database

The app uses **SQLite** locally by default (`data/rinse_rise.db`).

For **PostgreSQL on Railway**, set the `DATABASE_URL` environment variable. Tables are created automatically on first run.

### Railway setup

1. Create a [Railway](https://railway.app) project and add **PostgreSQL**
2. Deploy this repo (GitHub: `Rudra19-ui/Rinse-RiseBilling`)
3. In your **web service** → **Variables**, link Postgres or add `DATABASE_URL`
   - Railway injects this automatically when Postgres is linked to the web service
   - Use the URL Railway provides for the web service (the `*.railway.internal` host works **inside Railway only**)
4. Deploy — health check: `/api/health` should return `"database": "postgresql"`

### Local dev with Railway Postgres

Copy `.env.example` to `.env` and paste the **public** Postgres URL from Railway (host like `*.proxy.rlwy.net` or `*.railway.app` — **not** `postgres.railway.internal`).

```env
DATABASE_URL=postgresql://postgres:PASSWORD@HOST:PORT/railway
```

Then run `Start Billing.bat` as usual.

## Docker deployment

### Railway (recommended)

1. Push this repo to GitHub
2. Railway → **New Project** → **Deploy from GitHub** → select `Rinse-RiseBilling`
3. Add **PostgreSQL** and **link** it to the web service (`DATABASE_URL` is set automatically)
4. Railway detects `Dockerfile` and builds the container
5. Open the generated URL — app runs on port `8080` inside the container

Health check: `GET /api/health` → `{"ok": true, "database": "postgresql"}`

### Docker locally (with PostgreSQL)

Requires [Docker Desktop](https://www.docker.com/products/docker-desktop/).

```bash
docker compose up --build
```

Open [http://localhost:8080](http://localhost:8080)

Bills, Postgres, WhatsApp, and `/app/data` persist via Docker volumes. For **lower hosting cost** (same stack), see **[VPS_HOSTING.md](./VPS_HOSTING.md)**.

### Docker build only

```bash
docker build -t rinse-rise-billing .
docker run -p 8080:8080 -e DATABASE_URL="your-postgres-url" rinse-rise-billing
```

**Note:** Docker and Railway run the full stack (billing, PostgreSQL, WhatsApp bridge). Windows shop PCs can still use `Start Billing.bat` — don’t run local WhatsApp and Railway/VPS WhatsApp on the same account at once.

## Notes

- Database file `data/rinse_rise.db` is created locally and is not committed to git.
- After cloning, run `Start Billing.bat` once to install Python/Node dependencies.
