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
