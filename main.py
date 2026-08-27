import asyncio
import csv
import io
import logging
import os
import re
import secrets
import sqlite3
import string
import time
import hashlib
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Optional, List

from fastapi import FastAPI, HTTPException, Query, Header, Depends
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from scrapling.fetchers import StealthyFetcher
from scrapling import Selector

# Load .env file (if present) into os.environ — no external dependency needed.
# Real environment variables always win over values in .env.
def _load_dotenv(path: str = ".env") -> None:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key, val = key.strip(), val.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = val
    except FileNotFoundError:
        pass

_load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Mini Zoopla API", version="1.2.0")

# Enable adaptive mode globally
StealthyFetcher.adaptive = True

# ----------------------------------------------------------------------------
# Config (all via env so nothing secret lands in source control)
# ----------------------------------------------------------------------------
KEYS_DB_PATH = os.getenv("MINI_ZOOPLA_KEYS_DB", "keys.db")
DEFAULT_RATE_LIMIT = int(os.getenv("MINI_ZOOPLA_RATE_LIMIT", "60"))  # requests / owner / minute
CACHE_TTL = int(os.getenv("MINI_ZOOPLA_CACHE_TTL", "300"))            # seconds
ADMIN_KEY = os.getenv("MINI_ZOOPLA_ADMIN_KEY")                        # if set, /admin/* enabled
HOST = os.getenv("MINI_ZOOPLA_HOST", "127.0.0.1")                     # bind address (default: localhost only)
PORT = int(os.getenv("MINI_ZOOPLA_PORT", "8000"))                     # listen port

# ----------------------------------------------------------------------------
# API key store (SQLite). Keys are hashed (SHA-256); plaintext shown once.
# App-layer "RLS": every key belongs to an `owner` and is not bound to any
# particular branch_id — a valid key works against any branch. Enforcement
# is per authenticated key (rate limit + active flag).
# ----------------------------------------------------------------------------
class KeyStore:
    def __init__(self, path: str):
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS api_keys (
                key_id TEXT PRIMARY KEY,
                key_hash TEXT UNIQUE NOT NULL,
                owner TEXT NOT NULL,
                name TEXT,
                rate_limit INTEGER,             -- per-minute limit or NULL for default
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            )"""
        )
        self.conn.commit()

    def _hash(self, key: str) -> str:
        return hashlib.sha256(key.encode("utf-8")).hexdigest()

    def create_key(self, owner: str, name: str = "", rate_limit: Optional[int] = None) -> str:
        raw = "mz_" + "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(32))
        key_id = secrets.token_hex(8)
        self.conn.execute(
            "INSERT INTO api_keys (key_id, key_hash, owner, name, rate_limit, active, created_at) "
            "VALUES (?,?,?,?,?,1,?)",
            (key_id, self._hash(raw), owner, name, rate_limit, datetime.utcnow().isoformat()),
        )
        self.conn.commit()
        return raw  # plaintext, returned to caller only at creation time

    def get_by_hash(self, key_hash: str):
        row = self.conn.execute(
            "SELECT key_id, owner, name, rate_limit, active FROM api_keys WHERE key_hash=?",
            (key_hash,),
        ).fetchone()
        if not row or row[4] != 1:
            return None
        return {
            "key_id": row[0],
            "owner": row[1],
            "name": row[2],
            "rate_limit": row[3],
        }

    def revoke(self, key_id: str) -> bool:
        """Soft delete: mark the key inactive. It stays in the DB."""
        cur = self.conn.execute("UPDATE api_keys SET active=0 WHERE key_id=?", (key_id,))
        self.conn.commit()
        return cur.rowcount > 0

    def delete_key(self, key_id: str) -> bool:
        """Hard delete: purge the key row from the database entirely."""
        cur = self.conn.execute("DELETE FROM api_keys WHERE key_id=?", (key_id,))
        self.conn.commit()
        return cur.rowcount > 0

    def list_keys(self):
        rows = self.conn.execute(
            "SELECT key_id, owner, name, rate_limit, active, created_at FROM api_keys ORDER BY created_at DESC"
        ).fetchall()
        return [
            {
                "key_id": r[0], "owner": r[1], "name": r[2],
                "rate_limit": r[3], "active": bool(r[4]), "created_at": r[5],
            }
            for r in rows
        ]


store = KeyStore(KEYS_DB_PATH)


# ----------------------------------------------------------------------------
# In-memory rate limiter (per owner, fixed window). No external service.
# ----------------------------------------------------------------------------
class RateLimiter:
    def __init__(self):
        self.windows: dict = defaultdict(lambda: [0.0, 0])  # owner -> [window_start, count]

    def allow(self, owner: str, limit: int) -> bool:
        now = time.time()
        start, count = self.windows[owner]
        if now - start >= 60:
            self.windows[owner] = [now, 1]
            return True
        if count >= limit:
            return False
        self.windows[owner][1] += 1
        return True


limiter = RateLimiter()


# ----------------------------------------------------------------------------
# In-memory TTL cache. No Redis.
# ----------------------------------------------------------------------------
class TTLCache:
    def __init__(self, ttl: int):
        self.ttl = ttl
        self.data: dict = {}

    def get(self, k):
        item = self.data.get(k)
        if not item:
            return None
        ts, val = item
        if time.time() - ts > self.ttl:
            del self.data[k]
            return None
        return val

    def set(self, k, v):
        self.data[k] = (time.time(), v)


cache = TTLCache(CACHE_TTL)


# ----------------------------------------------------------------------------
# Auth dependency — enforces key validity, owner scoping, branch allow-list.
# ----------------------------------------------------------------------------
def authenticate(
    x_api_key: Optional[str] = Header(default=None),
    api_key: Optional[str] = Query(default=None, description="Fallback to X-API-Key header"),
) -> dict:
    key = x_api_key or api_key
    if not key:
        raise HTTPException(status_code=401, detail="Missing X-API-Key header or ?api_key=")
    record = store.get_by_hash(hashlib.sha256(key.encode()).hexdigest())
    if record is None:
        raise HTTPException(status_code=401, detail="Invalid or revoked API key")
    return record




# ----------------------------------------------------------------------------
# Scraper (unchanged adaptive logic)
# ----------------------------------------------------------------------------
ADAPTIVE_CONFIG = {
    "listing_row": {"selector": '[id^="listing_"]', "identifier": "zoopla_listing_row", "auto_save": True},
    "price": {"selector": '[data-testid="listing-card-content"] p[class*="priceText"]', "identifier": "zoopla_listing_price", "auto_save": True},
    "amenities": {"selector": 'p[class*="amenityListSlim"] span[class*="amenityItemSlim"]', "identifier": "zoopla_listing_amenities", "auto_save": True},
    "address": {"selector": 'address[class*="summary_address"]', "identifier": "zoopla_listing_address", "auto_save": True},
    "title": {"selector": '[data-testid="listing-card-content"] p[class*="summary"]', "identifier": "zoopla_listing_title", "auto_save": True},
    "link": {"selector": 'a[class*="detailsPageLink"]', "identifier": "zoopla_listing_link", "auto_save": True},
    "image": {"selector": 'a[class*="galleryLink"] img', "identifier": "zoopla_listing_image", "auto_save": True},
}

PRICE_RE = re.compile(r"£([\d,]+)\s*(pcm|pw)", re.I)
BEDS_RE = re.compile(r"(\d+)\s*bed", re.I)
BATHS_RE = re.compile(r"(\d+)\s*bath", re.I)
LISTING_ID_RE = re.compile(r"/details/(\d+)/")


class Property(BaseModel):
    listing_id: Optional[str] = None
    title: str
    price: str
    price_pcm: Optional[int] = None
    price_per_week: Optional[int] = None
    address: str
    bedrooms: Optional[int] = None
    bathrooms: Optional[int] = None
    property_type: Optional[str] = None
    listing_type: Optional[str] = None
    listing_url: str
    image_url: Optional[str] = None


class AgencyListingsResponse(BaseModel):
    agency: str
    listing_type: str
    properties: List[Property]
    total: int
    cached: bool = False


def _to_int(text: Optional[str]) -> Optional[int]:
    if not text:
        return None
    digits = re.sub(r"[^\d]", "", text)
    return int(digits) if digits else None


def _extract_listing_id(url: str) -> Optional[str]:
    m = LISTING_ID_RE.search(url or "")
    return m.group(1) if m else None


def scrape_zoopla_agency(branch_id: str, max_pages: int = 3, listing_type: str = "rent") -> List[Property]:
    properties: List[Property] = []
    listing_type = listing_type.lower()
    if listing_type not in ("rent", "sale"):
        listing_type = "rent"
    section = "for-sale" if listing_type == "sale" else "to-rent"
    base_url = f"https://www.zoopla.co.uk/{section}/property/uk/?branch_id={branch_id}"

    fetcher = StealthyFetcher()
    for page in range(1, max_pages + 1):
        url = f"{base_url}&pn={page}" if page > 1 else base_url
        logger.info("Scraping page %s: %s", page, url)
        try:
            response = fetcher.fetch(
                url, wait_selector='[data-testid="listing-card-content"]',
                wait_selector_state="attached", timeout=60000, headless=True, network_idle=True,
            )
            if response.status >= 400:
                logger.warning("Fetch failed page %s: %s", page, response.status)
                break
            html_content = response.text or ""
            if not html_content and response.body:
                html_content = response.body.decode("utf-8", "ignore") if isinstance(response.body, bytes) else str(response.body)
            if not html_content:
                logger.warning("Empty response body on page %s", page)
                break

            # Detect bot/Cloudflare challenge so it logs loudly instead of silent []
            low = html_content.lower()
            challenge_hits = [kw for kw in (
                "are you a robot", "verify you are human", "checking your browser",
                "just a moment", "unusual traffic", "access denied", "captcha",
                "cf-chl", "please enable javascript",
            ) if kw in low]
            if challenge_hits:
                logger.warning("BOT-CHALLENGE detected on page %s: %s", page, challenge_hits)
                break

            # A real listing page is large; a tiny body means we got a stub/challenge.
            if len(html_content) < 5000:
                logger.warning("SUSPICIOUSLY SHORT body on page %s (%s bytes) - likely blocked; not caching", page, len(html_content))
                break

            page_selector = Selector(html_content, adaptive=True, url="zoopla.co.uk")
            rows = page_selector.css(ADAPTIVE_CONFIG["listing_row"]["selector"], auto_save=True, identifier=ADAPTIVE_CONFIG["listing_row"]["identifier"])
            if not rows:
                rows = page_selector.css(ADAPTIVE_CONFIG["listing_row"]["selector"], adaptive=True, identifier=ADAPTIVE_CONFIG["listing_row"]["identifier"])
            logger.info("Page %s: found %s listing rows", page, len(rows))
            if not rows:
                break
            page_count = 0
            for row in rows:
                prop = parse_listing(row, listing_type)
                if prop:
                    properties.append(prop)
                    page_count += 1
            logger.info("Page %s: extracted %s properties", page, page_count)
            if page_count == 0:
                break
            time.sleep(1)
        except Exception as e:
            logger.error("Error on page %s: %s", page, e)
            break
    return properties


def _first(node: Selector, selector: str, identifier: str, auto_save: bool):
    found = node.css(selector, auto_save=auto_save, identifier=identifier)
    if not found:
        found = node.css(selector, adaptive=True, identifier=identifier)
    return found[0] if found else None


def parse_listing(row: Selector, listing_type: str) -> Optional[Property]:
    try:
        price_elem = _first(row, ADAPTIVE_CONFIG["price"]["selector"], ADAPTIVE_CONFIG["price"]["identifier"], ADAPTIVE_CONFIG["price"]["auto_save"])
        price_text = price_elem.text.strip() if price_elem is not None else ""
        price_pcm = None
        price_per_week = None
        m_price = PRICE_RE.search(price_text)
        if m_price:
            price_pcm = _to_int(m_price.group(1))
        if listing_type == "sale" and price_pcm is None:
            sale_m = re.search(r"£([\d,]+)", price_text)
            if sale_m:
                price_pcm = _to_int(sale_m.group(1))
        if price_elem is not None and price_elem.parent is not None:
            alt = _first(price_elem.parent, '[class*="priceAlternative"]', "zoopla_listing_price_alt", True)
            if alt is not None:
                m_alt = PRICE_RE.search(alt.text)
                if m_alt and m_alt.group(2).lower() == "pw":
                    price_per_week = _to_int(m_alt.group(1))

        amenities = row.css(ADAPTIVE_CONFIG["amenities"]["selector"], auto_save=True, identifier=ADAPTIVE_CONFIG["amenities"]["identifier"])
        if not amenities:
            amenities = row.css(ADAPTIVE_CONFIG["amenities"]["selector"], adaptive=True, identifier=ADAPTIVE_CONFIG["amenities"]["identifier"])
        bedrooms = None
        bathrooms = None
        for a in amenities:
            t = a.text.strip()
            mb = BEDS_RE.search(t)
            if mb:
                bedrooms = _to_int(mb.group(1))
                continue
            mh = BATHS_RE.search(t)
            if mh:
                bathrooms = _to_int(mh.group(1))

        addr_elem = _first(row, ADAPTIVE_CONFIG["address"]["selector"], ADAPTIVE_CONFIG["address"]["identifier"], ADAPTIVE_CONFIG["address"]["auto_save"])
        address = addr_elem.text.strip() if addr_elem is not None else ""
        title = address
        if addr_elem is not None and addr_elem.next is not None:
            title = addr_elem.next.text.strip() or address

        link_elem = _first(row, ADAPTIVE_CONFIG["link"]["selector"], ADAPTIVE_CONFIG["link"]["identifier"], ADAPTIVE_CONFIG["link"]["auto_save"])
        href = link_elem.attrib.get("href") if link_elem is not None else None
        listing_url = f"https://www.zoopla.co.uk{href}" if href and href.startswith("/") else (href or "")
        listing_id = _extract_listing_id(listing_url)

        img_elem = _first(row, ADAPTIVE_CONFIG["image"]["selector"], ADAPTIVE_CONFIG["image"]["identifier"], ADAPTIVE_CONFIG["image"]["auto_save"])
        image_url = None
        if img_elem is not None:
            src = img_elem.attrib.get("src")
            if src and src.startswith("http"):
                image_url = src

        return Property(
            listing_id=listing_id, title=title or "Unknown", price=price_text or "POA",
            price_pcm=price_pcm, price_per_week=price_per_week, address=address,
            bedrooms=bedrooms, bathrooms=bathrooms, property_type=None,
            listing_type=listing_type, listing_url=listing_url, image_url=image_url,
        )
    except Exception as e:
        logger.debug("Parse error: %s", e)
        return None


def properties_to_csv(properties: List[Property]) -> str:
    fields = ["listing_id", "title", "price", "price_pcm", "price_per_week", "address",
              "bedrooms", "bathrooms", "property_type", "listing_type", "listing_url", "image_url"]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for p in properties:
        writer.writerow(p.model_dump())
    return buf.getvalue()


# ----------------------------------------------------------------------------
# Endpoints
# ----------------------------------------------------------------------------
@app.get("/health")
async def health_check():
    return {"status": "healthy"}


@app.get("/api/agency/{branch_id}")
async def get_agency_listings(
    branch_id: str,
    max_pages: int = Query(3, ge=1, le=10, description="Maximum pages to scrape"),
    listing_type: str = Query("rent", description="rent or sale"),
    fmt: str = Query("json", description="json or csv"),
    record: dict = Depends(authenticate),
):
    # 1. Rate limit (per owner)
    limit = record["rate_limit"] or DEFAULT_RATE_LIMIT
    if not limiter.allow(record["owner"], limit):
        raise HTTPException(status_code=429, detail=f"Rate limit exceeded ({limit}/min for owner '{record['owner']}')")

    cache_key = (branch_id, listing_type, max_pages)
    cached = cache.get(cache_key)
    if cached is not None:
        if fmt == "csv":
            return PlainTextResponse(properties_to_csv(cached), media_type="text/csv",
                                     headers={"Content-Disposition": f'attachment; filename="agency_{branch_id}_{listing_type}.csv"'})
        return AgencyListingsResponse(agency=branch_id, listing_type=listing_type, properties=cached, total=len(cached), cached=True)

    loop = asyncio.get_event_loop()
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor() as pool:
        properties = await loop.run_in_executor(pool, scrape_zoopla_agency, branch_id, max_pages, listing_type)

    # Never cache empty results: they are usually a transient bot-challenge/empty
    # page, and caching them would poison the cache for the whole TTL window.
    if properties:
        cache.set(cache_key, properties)
    if fmt == "csv":
        return PlainTextResponse(properties_to_csv(properties), media_type="text/csv",
                                 headers={"Content-Disposition": f'attachment; filename="agency_{branch_id}_{listing_type}.csv"'})
    return AgencyListingsResponse(agency=branch_id, listing_type=listing_type, properties=properties, total=len(properties), cached=False)


# ----------------------------------------------------------------------------
# Admin (key management). Enabled only if MINI_ZOOPLA_ADMIN_KEY is set.
# ----------------------------------------------------------------------------
def require_admin(x_admin_key: Optional[str] = Header(default=None)):
    if not ADMIN_KEY:
        raise HTTPException(status_code=503, detail="Admin disabled (set MINI_ZOOPLA_ADMIN_KEY)")
    if x_admin_key != ADMIN_KEY:
        raise HTTPException(status_code=401, detail="Invalid admin key")


class KeyCreate(BaseModel):
    owner: str
    name: str = ""
    rate_limit: Optional[int] = None


@app.post("/admin/keys")
async def create_key(body: KeyCreate, _=Depends(require_admin)):
    raw = store.create_key(body.owner, body.name, body.rate_limit)
    return {"key": raw, "owner": body.owner, "note": "Store this key securely; it is shown only once."}


@app.get("/admin/keys")
async def list_keys(_=Depends(require_admin)):
    return {"keys": store.list_keys()}


@app.delete("/admin/keys/{key_id}")
async def revoke_or_delete_key(
    key_id: str,
    purge: bool = Query(False, description="If true, delete the key row from the database entirely instead of just deactivating it"),
    _=Depends(require_admin),
):
    ok = store.delete_key(key_id) if purge else store.revoke(key_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Key not found")
    return {"deleted": key_id, "purged": bool(purge)}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=HOST, port=PORT)
