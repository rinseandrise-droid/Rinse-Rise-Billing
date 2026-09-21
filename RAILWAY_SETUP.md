# Railway — keep data & WhatsApp connected after every deploy

Every **git push / redeploy** replaces the container. Without persistent storage, bills, customer history, and WhatsApp sessions are **lost**.

## One-time setup (required)

### 1 — Persistent volume (bills + WhatsApp session)

1. Railway → **Rinse-RiseBilling** → **Volumes** → **Add Volume**
2. **Mount path:** `/app/data`
3. **Size:** 1 GB (enough for database + WhatsApp auth)
4. Save and **Redeploy**

This keeps:
- `rinse_rise.db` — all bills & customer history (SQLite fallback)
- `whatsapp-auth/` — WhatsApp login (scan QR **once**)
- `invoices/` — generated PDFs

### 2 — PostgreSQL (recommended for production)

1. Add **PostgreSQL** service in the same Railway project
2. **Rinse-RiseBilling** → **Variables** → **delete any old** `DATABASE_URL` (especially if it contains `postgres.railway.internal`)
3. Add these variables (pick **one** approach):

**Option A — Variable references (recommended)**

| Name | Value |
|------|--------|
| `DATABASE_URL` | Reference → Postgres → `DATABASE_PRIVATE_URL` |
| `DATABASE_PUBLIC_URL` | Reference → Postgres → `DATABASE_PUBLIC_URL` |

**Option B — Public URL only (if internal DNS fails)**

| Name | Value |
|------|--------|
| `DATABASE_URL` | Paste **DATABASE_PUBLIC_URL** from Postgres (host like `*.proxy.rlwy.net`) |
| `DATABASE_PUBLIC_URL` | Same public URL (optional fallback) |

The app **prefers `DATABASE_PUBLIC_URL`** when both are set. Never commit real passwords to git — set them only in Railway Variables.

4. **Redeploy**

With Postgres linked, all bills persist in the cloud database (even without the volume). The volume still helps for WhatsApp session files.

### 3 — Memory for WhatsApp

- Service **RAM:** at least **1 GB** (2 GB is better for faster QR)
- Variable `WHATSAPP_ENABLED=1` (default in Docker image)
- The container runs a small **health proxy** on port 3001 so the billing page shows scanner progress immediately; the real scanner runs on port 3002 inside the same container

## Verify after deploy

Open: `https://YOUR-APP.up.railway.app/api/health`

Look for:

```json
{
  "dbOk": true,
  "backend": "postgresql",
  "whatsappAvailable": true,
  "whatsappReady": true,
  "persistence": {
    "dataDir": "/app/data",
    "sqliteDbExists": true,
    "whatsappSessionSaved": true,
    "volumeMountPath": "/app/data"
  }
}
```

## Common mistakes

| Mistake | Result |
|--------|--------|
| No volume at `/app/data` | Bills & WhatsApp reset on every push |
| Pasted old `DATABASE_URL` | Database errors, data not saved |
| Postgres not linked | SQLite used but lost without volume |
| `< 512 MB RAM` | WhatsApp disconnects / crashes |
| Scanning QR after every deploy | No volume for `whatsapp-auth` |

## WhatsApp stays connected

- Scan QR **once** after volume is mounted
- Do **not** click Reset Connection unless needed
- After deploy, wait **2–3 minutes** — status shows **Restoring saved session** (no scan needed)
- Do **not** run **Start Billing.bat** on your PC at the same time as Railway — two bridges on the same WhatsApp account will kick each other off
- One bridge runs in the container (no duplicate sessions)
- Session files live in `/app/data/whatsapp-auth`
- Service RAM: **at least 1 GB** (512 MB causes Chromium crashes → reconnect loops)

## Local development

Run **Start Billing.bat** — data stays in `data/rinse_rise.db` on your PC (not affected by Railway deploys).
