# Mini Zoopla API

A small FastAPI service that scrapes Zoopla listings for a specific agency branch,
using [scrapling](https://scrapling.readthedocs.io/) with **adaptive** selectors that
auto-heal when Zoopla changes its layout.

---

## Endpoints

| Method | URL | Description |
|--------|-----|-------------|
| GET | `/health` | Liveness check |
| GET | `/api/agency/{branch_id}` | Listings for a branch (JSON) |
| GET | `/api/agency/{branch_id}?listing_type=sale` | For-sale listings |
| GET | `/api/agency/{branch_id}?fmt=csv` | Same data as CSV |

> `{branch_id}` is the numeric Zoopla branch id, e.g. `12345`. To find it, open the
> agency's branch page on Zoopla and copy the number from the `branch_id=` query
> parameter (or the `find-agents/branch/...-<id>/` slug).

Query params:
- `max_pages` (1-10, default 3) — how many pagination pages to crawl
- `listing_type` — `rent` (default) or `sale`
- `fmt` — `json` (default) or `csv`

Examples:
```
GET /api/agency/12345?max_pages=2&listing_type=sale&fmt=csv
GET /api/agency/12345                  # rent, JSON, first 3 pages
```

Response fields per property: `listing_id, title, price, price_pcm, price_per_week,
address, bedrooms, bathrooms, property_type, listing_type, listing_url, image_url`.

---

## Setup

You need **Python 3.10+**. The scraper uses a headless browser (via scrapling/patchright),
so the first install also downloads a browser binary.

### Configuration (env vars / `.env`)

All settings are read from environment variables, and a `.env` file in the project
root is loaded automatically (real shell env vars always win). Copy the template and
edit it:

```bash
cp .env.example .env
```

| Variable | Default | Purpose |
|----------|---------|---------|
| `MINI_ZOOPLA_HOST` | `127.0.0.1` | Bind address. Defaults to **localhost only** (safe — Cloudflare Tunnel reaches it). Set `0.0.0.0` only if you intentionally expose the port directly. |
| `MINI_ZOOPLA_PORT` | `8000` | Listen port. |
| `MINI_ZOOPLA_ADMIN_KEY` | _(empty)_ | Required to enable `/admin/*` key management. Generate with `openssl rand -hex 24`. |
| `MINI_ZOOPLA_RATE_LIMIT` | `60` | Default requests/minute per key owner. |
| `MINI_ZOOPLA_CACHE_TTL` | `300` | Response cache TTL (seconds) — avoids re-scraping the same branch. |
| `MINI_ZOOPLA_KEYS_DB` | `keys.db` | Path to the API-key store (SQLite). |

> Never commit your real `.env` — it's gitignored. Only `.env.example` is tracked.

### Windows

Using **PowerShell** (recommended) or the same commands in a normal terminal:

```powershell
# 1. Clone / copy the project, then enter it
cd D:\LocalProjects\MiniZooplaAPI

# 2. Create a virtual environment
python -m venv venv

# 3. Install dependencies
venv\Scripts\pip install -r requirements.txt

# 4. Download the stealth browser (patchright) — runs once
venv\Scripts\python -m patchright install chromium
#    (or: venv\Scripts\python -m playwright install chromium)

# 5. Run
venv\Scripts\python main.py
#    -> http://localhost:8000
```

> If `python` isn't on PATH, use `py -3` or the full path to your Python.

### macOS

```bash
# 1. Enter the project
cd ~/Projects/MiniZooplaAPI      # or wherever you put it

# 2. Create a virtual environment
python3 -m venv venv

# 3. Install dependencies
venv/bin/pip install -r requirements.txt

# 4. Download the stealth browser (needs Homebrew for some system libs)
#    If you don't have Homebrew: https://brew.sh
venv/bin/python -m patchright install chromium
#    (or: venv/bin/python -m playwright install chromium)

# 5. Run
venv/bin/python main.py
#    -> http://localhost:8000
```

> macOS may need `xcode-select --install` the first time for build tools.
> On Apple Silicon you might also need `brew install openssl` if curl_cffi complains.

### Linux (VPS / server)

Tested on Ubuntu 22.04 / 24.04. Debian is the same.

```bash
# 1. System deps + Python
sudo apt update
sudo apt install -y python3-venv python3-pip

# 2. Clone and enter the project
git clone <your-repo-url> /opt/minizoopla
cd /opt/minizoopla

# 3. Virtual environment
python3 -m venv venv

# 4. Install Python deps
venv/bin/pip install -r requirements.txt

# 5. Download the stealth browser
venv/bin/python -m patchright install chromium
#    (or: venv/bin/python -m playwright install chromium)

# 6. Run (foreground, for testing)
venv/bin/python main.py
#    -> http://localhost:8000
```

Verify it's up:
```bash
curl http://localhost:8000/health     # -> {"status":"healthy"}
```

> Keep the venv path stable: `/opt/minizoopla/venv/bin/python`. The systemd unit
> below assumes exactly this layout.

---

## Running it as a service (Linux VPS)

So it survives reboots and runs headless. Save to `/etc/systemd/system/minizoopla.service`:

```ini
[Unit]
Description=Mini Zoopla API
After=network.target

[Service]
User=www-data
WorkingDirectory=/opt/minizoopla
ExecStart=/opt/minizoopla/venv/bin/python main.py
Restart=always
RestartSec=5
# Bind is localhost by default (127.0.0.1). Set secrets here, never in source control.
Environment=MINI_ZOOPLA_HOST=127.0.0.1
Environment=MINI_ZOOPLA_PORT=8000
Environment=MINI_ZOOPLA_ADMIN_KEY=REPLACE_WITH_openssl_rand_-hex_24
Environment=MINI_ZOOPLA_RATE_LIMIT=60
Environment=MINI_ZOOPLA_CACHE_TTL=300

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now minizoopla
sudo systemctl status minizoopla      # should show "active (running)"
curl http://localhost:8000/health
```

The app binds `0.0.0.0:8000` but is localhost-only — Cloudflare Tunnel is what makes
it public. No firewall ports need to be opened.

### Run it with PM2 (alternative to systemd)

PM2 is handy if you already use it for other Node services on the VPS, or prefer not
to write a systemd unit. Works on Linux/macOS/Windows (WSL).

```bash
# Install PM2 (once, globally)
sudo npm install -g pm2          # or: npm install -g pm2  (no sudo if using nvm)

# From the project dir, start the API under PM2
pm2 start "venv/bin/python main.py" --name mini-zoopla \
  --interpreter none \
  -- \
  && pm2 save                      # persist so it comes back after reboot
sudo pm2 startup                   # (Linux) enable the PM2 respawn service on boot
```

Pass environment variables (admin key, limits) via an ecosystem file instead of the
shell so they aren't visible in `ps`. Save as `ecosystem.config.cjs`:

```javascript
module.exports = {
  apps: [{
    name: 'mini-zoopla',
    cwd: '/opt/minizoopla',
    script: '/opt/minizoopla/venv/bin/python',
    args: 'main.py',
    interpreter: 'none',
    instances: 1,
    exec_mode: 'fork',
    autorestart: true,
    watch: false,
    max_memory_restart: '1G',
    env: {
      // Set secrets OUTSIDE source control, e.g. in your shell / a non-committed .env
      MINI_ZOOPLA_HOST: process.env.MINI_ZOOPLA_HOST || '127.0.0.1',
      MINI_ZOOPLA_PORT: process.env.MINI_ZOOPLA_PORT || '8000',
      MINI_ZOOPLA_ADMIN_KEY: process.env.MINI_ZOOPLA_ADMIN_KEY,
      MINI_ZOOPLA_RATE_LIMIT: process.env.MINI_ZOOPLA_RATE_LIMIT || '60',
      MINI_ZOOPLA_CACHE_TTL: process.env.MINI_ZOOPLA_CACHE_TTL || '300',
    },
  }],
};
```

```bash
# Load env first (never commit the file), then start:
export MINI_ZOOPLA_ADMIN_KEY="$(openssl rand -hex 24)"
pm2 start ecosystem.config.cjs
pm2 save
```

Useful PM2 commands:
```bash
pm2 ls                      # list processes
pm2 logs mini-zoopla        # tail logs
pm2 restart mini-zoopla
pm2 delete mini-zoopla
```

> Note: the API runs a headless browser per scrape, so keep `instances: 1` (fork mode).
> Horizontal scaling would need a shared cache/key store; for a personal API this is
> plenty.

---

## Exposing it via Cloudflare Tunnel (cloudflared)

### Install cloudflared (CLI)

**Linux (amd64):**
```bash
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -o /usr/local/bin/cloudflared
chmod +x /usr/local/bin/cloudflared
cloudflared --version
```

**macOS:**
```bash
brew install cloudflared
```

**Windows (PowerShell):**
```powershell
winget install Cloudflare.cloudflared
# or download the .exe from https://github.com/cloudflare/cloudflared/releases/latest
```

### Option A — Quick tunnel (no Cloudflare account)

```bash
cloudflared tunnel --url http://localhost:8000
```

Prints a `https://*.trycloudflare.com` URL you can call from anywhere. Great for testing.
The URL changes every time you restart the tunnel.

### Option B — Named tunnel (stable URL, recommended)

```bash
cloudflared login            # browser opens; pick your Cloudflare zone
cloudflared tunnel create my-zoopla-tunnel
```

Create `/etc/cloudflared/config.yml` (Linux/macOS) or `%USERPROFILE%\.cloudflared\config.yml` (Windows):

```yaml
tunnel: my-zoopla-tunnel
credentials-file: /root/.cloudflared/<tunnel-id>.json   # adjust path to your home on macOS/Windows

ingress:
  - hostname: zoopla.yourdomain.com
    service: http://localhost:8000
  - service: http://localhost:8000
```

Run as a service:

```bash
# Linux
sudo cloudflared service install
sudo systemctl enable --now cloudflared

# macOS (homebrew)
sudo cloudflared service install

# Windows — run cloudflared as a scheduled task / start it in a minimized window:
cloudflared tunnel run my-zoopla-tunnel
```

Finally, in the Cloudflare DNS dashboard, add a **CNAME** `zoopla.yourdomain.com` →
`<tunnel-id>.cfargotunnel.com`.

You can now call:
```
https://your-tunnel.yourdomain.com/api/agency/12345?listing_type=sale&fmt=csv
```

---

## Authentication, API keys & rate limiting

Every `/api/agency/{branch_id}` call requires an API key in the `X-API-Key` header.
Keys are stored in a local SQLite file (`keys.db`, gitignored — never committed). The
key itself is **hashed** (SHA-256); the plaintext is shown only once at creation.

### How "RLS" works here (app-layer, not Postgres)

This project deliberately avoids Postgres/Redis to stay lightweight. Instead, every
key is scoped at the **application layer**:

- `owner` — who the key belongs to (used for rate-limit accounting + auditing).
- `allowed_branches` — optional comma-separated list of branch ids the key may query.
  A key with `allowed_branches=["12345"]` gets `403` on any other branch. Empty/`""`
  means "any branch".
- `rate_limit` — per-owner requests/minute. Defaults to `MINI_ZOOPLA_RATE_LIMIT` (60).
- `active` — soft-delete via revoke (no row deletion, so audit history survives).

This gives you per-client isolation (the "R" in RLS) without a separate database
service. If you later need true PostgreSQL Row-Level Security, swap `KeyStore` for a
Postgres backend — the `authenticate`/`enforce_branch_access` logic stays the same.

### Configure the server

```bash
# Required to enable key management endpoints (/admin/*):
export MINI_ZOOPLA_ADMIN_KEY="$(openssl rand -hex 24)"   # the one key that can mint others

# Optional tuning (with sane defaults):
export MINI_ZOOPLA_RATE_LIMIT="60"     # default req/min per owner
export MINI_ZOOPLA_CACHE_TTL="300"     # seconds; cached responses skip re-scraping
export MINI_ZOOPLA_KEYS_DB="keys.db"   # path to the key store
```

### Create and manage keys (admin)

```bash
# Create a key scoped to one branch for a Google Sheets user
curl -X POST https://your-tunnel.yourdomain.com/admin/keys \
  -H "X-Admin-Key: $MINI_ZOOPLA_ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{"owner":"sheets_user","name":"google_sheets","allowed_branches":["12345"],"rate_limit":60}'

# -> {"key":"mz_xxxx...","owner":"sheets_user","note":"Store this key securely; it is shown only once."}

# List keys (hashes never returned)
curl https://your-tunnel.yourdomain.com/admin/keys -H "X-Admin-Key: $MINI_ZOOPLA_ADMIN_KEY"

# Revoke a key (soft delete)
curl -X DELETE https://your-tunnel.yourdomain.com/admin/keys/<key_id> \
  -H "X-Admin-Key: $MINI_ZOOPLA_ADMIN_KEY"
```

### Call the API with a key

```bash
curl https://your-tunnel.yourdomain.com/api/agency/12345?fmt=csv \
  -H "X-API-Key: mz_xxxx..."
```

In Google Sheets:
```
=IMPORTDATA("https://your-tunnel.yourdomain.com/api/agency/12345?fmt=csv")
```
and add the header via Apps Script, or pass the key as a query param if you prefer:
the endpoint also accepts `?api_key=mz_xxxx...` as a fallback to the header.

> Security notes:
> - Never commit `keys.db` or `MINI_ZOOPLA_ADMIN_KEY`. Both are in `.gitignore`.
> - Rotate the admin key by changing the env var and revoking old client keys.
> - Rate limiting is an in-memory fixed window (per owner); it resets on restart and is
>   not shared across multiple API processes. Fine for a single-instance personal API.

---

## Using it from Google Sheets

CSV (easiest — pastes straight into cells):
```
=IMPORTDATA("https://your-tunnel.yourdomain.com/api/agency/12345?fmt=csv")
```
or for a single cell of raw text:
```
=WEBSERVICE("https://your-tunnel.yourdomain.com/api/agency/12345?fmt=csv")
```
then `SPLIT` by comma, or use `IMPORTDATA` which handles the CSV natively.

JSON:
```
=WEBSERVICE("https://your-tunnel.yourdomain.com/api/agency/12345")
```
then parse with the `IMPORTJSON` Apps Script, or `FILTERXML` on the XML-ized body.

---

## Notes

- Adaptive selectors save fingerprints to a local SQLite DB on the **first successful
  run** (`auto_save=True`). Later runs use `adaptive=True` to recover from layout drift,
  so you rarely need to touch the selectors after a Zoopla redesign.
- Be respectful: the scraper sleeps 1s between pages. Don't hammer Zoopla.
- `patchright install chromium` (or `playwright install chromium`) must run on **every
  machine** you deploy to — the headless browser is not part of the pip package.
- The service user (`www-data`) needs read+write access to the project dir for the
  adaptive-selector SQLite DB. If you hit permission errors, `chown -R www-data:www-data /opt/minizoopla`.
