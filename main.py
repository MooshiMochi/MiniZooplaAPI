import asyncio
import csv
import io
import json
import logging
import os
import re
import secrets
import sqlite3
import string
import sys
import threading
import time
import itertools
import concurrent.futures
import atexit
import hashlib
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Optional, List

from fastapi import FastAPI, HTTPException, Query, Header, Depends
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from scrapling.fetchers import StealthyFetcher, StealthySession
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

# Route DEBUG/INFO to stdout and WARNING+ (incl. ERROR) to stderr, so pm2's
# stdout/stderr split is meaningful. Configured on the root logger so third-party
# loggers (scrapling, uvicorn, etc.) inherit it too.
class _StdoutFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno <= logging.INFO


_root = logging.getLogger()
_root.setLevel(logging.DEBUG)
_stdout = logging.StreamHandler(sys.stdout)
_stdout.addFilter(_StdoutFilter())
_stderr = logging.StreamHandler(sys.stderr)
_stderr.setLevel(logging.WARNING)
_formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
_stdout.setFormatter(_formatter)
_stderr.setFormatter(_formatter)
_root.addHandler(_stdout)
_root.addHandler(_stderr)

logger = logging.getLogger(__name__)

app = FastAPI(title="Mini Zoopla API", version="1.3.0")

# Enable adaptive mode globally
StealthyFetcher.adaptive = True

# Parser arguments shared by every StealthySession fetch (computed once, not
# per request). Mirrors what StealthyFetcher.fetch() injects via
# cls._generate_parser_arguments(); we reuse the same dict for the persistent
# session so behaviour matches the original per-call Fetcher exactly.
_STEALTH_PARSER_ARGS = StealthyFetcher._generate_parser_arguments()

# ============================================================================
# Persistent browser session pool
# ----------------------------------------------------------------------------
# Zoopla sits behind Cloudflare, so we *must* use a stealthy browser — a plain
# HTTP request returns 403 "Just a moment". StealthyFetcher.fetch() launches a
# fresh Chromium per call, which dominates latency.
#
# Playwright's *synchronous* API binds a greenlet to the OS thread that created
# the browser. A browser may therefore only be driven from the thread that
# built it (otherwise: "cannot switch to a different thread"). So instead of one
# shared browser, we keep a POOL of `MINI_ZOOPLA_BROWSER_WORKERS` browsers, each
# owned by its own single-threaded worker. The search fetch and every detail
# fetch are dispatched to a worker thread, so:
#   * only `MINI_ZOOPLA_BROWSER_WORKERS` browser launches happen per process
#   * the N detail-page fetches run CONCURRENTLY across the pool, so latency is
#     ~one fetch instead of N sequential fetches (well under 5s on warm calls).
# The pool is lazily warmed on first use and rebuilt if a worker dies.
# ============================================================================
BROWSER_WORKERS = int(os.getenv("MINI_ZOOPLA_BROWSER_WORKERS", "4"))

# One executor per worker thread. Each executor has exactly one worker so its
# browser is always driven from the same thread. Builds/starts and every .fetch
# are submitted to the owning executor.
_BROWSER_EXECUTORS: List[concurrent.futures.ThreadPoolExecutor] = []
_BROWSER_SESSIONS: List[Optional["StealthySession"]] = []
_POOL_LOCK = threading.RLock()
_next_worker = itertools.count().__next__  # round-robin cursor


def _build_session_impl() -> "StealthySession":
    """Create + start a persistent stealthy session (runs on its owner thread)."""
    session = StealthySession(
        headless=True,
        network_idle=True,
        solve_cloudflare=True,
        # A few ready tabs so a burst of fetches on this one worker doesn't queue.
        max_pages=4,
    )
    session.start()
    return session


def _ensure_pool() -> None:
    """Lazily build the worker pool on first use (idempotent, thread-safe)."""
    global _BROWSER_EXECUTORS, _BROWSER_SESSIONS
    with _POOL_LOCK:
        if _BROWSER_EXECUTORS:
            return
        executors = []
        sessions = []
        for i in range(max(1, BROWSER_WORKERS)):
            ex = concurrent.futures.ThreadPoolExecutor(
                max_workers=1, thread_name_prefix=f"zoopla-browser-{i}"
            )
            executors.append(ex)
            sessions.append(ex.submit(_build_session_impl).result())
        _BROWSER_EXECUTORS = executors
        _BROWSER_SESSIONS = sessions


def _repair_session(idx: int) -> None:
    """Rebuild a dead worker's browser (caller holds _POOL_LOCK)."""
    try:
        old = _BROWSER_SESSIONS[idx]
        if old is not None:
            _BROWSER_EXECUTORS[idx].submit(old.close).result(timeout=30)
    except Exception:
        pass
    _BROWSER_SESSIONS[idx] = _BROWSER_EXECUTORS[idx].submit(_build_session_impl).result()


def _get_worker() -> int:
    """Return the index of the next worker in round-robin order."""
    _ensure_pool()
    return _next_worker() % len(_BROWSER_EXECUTORS)


def _fetch_on_worker(idx: int, url: str, **kwargs) -> "Response":
    """Run a fetch on worker `idx`'s browser (on its own thread)."""
    with _POOL_LOCK:
        session = _BROWSER_SESSIONS[idx]
        if session is None or not getattr(session, "_is_alive", False):
            _repair_session(idx)
            session = _BROWSER_SESSIONS[idx]
    # Submit the navigation to the worker's thread (keeps the greenlet in-place).
    return _BROWSER_EXECUTORS[idx].submit(lambda: session.fetch(url, **kwargs)).result()


def get_session() -> "StealthySession":
    """Process-wide warm browser. Returns the next pooled session; callers that
    need a specific browser should use fetch_via_browser() instead."""
    _ensure_pool()
    return _BROWSER_SESSIONS[_next_worker() % len(_BROWSER_SESSIONS)]


def fetch_via_browser(url: str, **kwargs) -> "Response":
    """Fetch a URL on the next available worker's browser (round-robin)."""
    return _fetch_on_worker(_get_worker(), url, **kwargs)


def close_session() -> None:
    """Tear down the whole worker pool (e.g. on shutdown)."""
    global _BROWSER_EXECUTORS, _BROWSER_SESSIONS
    with _POOL_LOCK:
        executors, sessions = _BROWSER_EXECUTORS, _BROWSER_SESSIONS
        _BROWSER_EXECUTORS, _BROWSER_SESSIONS = [], []
    for ex, sess in zip(executors, sessions):
        if sess is not None:
            try:
                ex.submit(sess.close).result(timeout=30)
            except Exception:
                pass
        ex.shutdown(wait=False)


# Cleanly shut down the browser pool when the process exits.
atexit.register(close_session)


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
# Scraper
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

# Regexes for detail-page text extraction
AVAILABLE_RE = re.compile(r"(?:Available\s+(?:from|now|end\s+of)\s+(?:\d{1,2}[/]\d{1,2}[/]\d{2,4}|[A-Z][a-z]+\s+\d{4}|\d{4}))|Just\s+added", re.I)
FURNISHED_LABEL_RE = re.compile(r"^\s*(Furnished|Unfurnished|Part\s+Furnished|Part-furnished|Semi-furnished|Semi\s+Furnished|Furnished\s+or\s+Unfurnished|Unfurnished\s+or\s+Furnished)\s*$", re.I)
EPC_RE = re.compile(r"EPC\s+Rating:\s*([A-H])\b", re.I)
SIZE_RE = re.compile(r"(\d{2,4})\s*(?:sq\.?\s*ft|square\s*(?:feet|ft)|sqft)", re.I)
DEPOSIT_RE = re.compile(r"Deposit:\s*£?([\d,]+(?:[.,]\d{2})?)", re.I)
COUNCIL_TAX_RE = re.compile(r"Council\s+Tax\s+Band:\s*([A-Ja-j]|Not\s+yet\s+known|Tbc|TBC|To\s+be\s+confirmed)", re.I)
HOLDING_DEPOSIT_RE = re.compile(r"Holding\s+Deposit:\s*£?([\d,]+(?:[.,]\d{2})?)", re.I)

# Keywords for inferring boolean flags from features + description text
PARKING_KEYWORDS = re.compile(r"\b(parking|car\s+parking|driveway|garage|off-street\s+parking|allocated\s+parking|secure\s+parking|on-street\s+parking|parking\s+space)\b", re.I)
OUTDOOR_KEYWORDS = re.compile(r"\b(garden|patio|yard|outdoor\s+space|terrace|balcony|decking|communal\s+garden|green\s+space|rear\s+garden|front\s+garden|brick\s+work|courtyard)\b", re.I)
BILLS_KEYWORDS = re.compile(r"\b(bills?\s+inclusive|bills?\s+included|all\s+bills?\s+inclusive|utility\s+bills?\s+included|council\s+tax\s+included)\b", re.I)


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

    # ------------------------------------------------------------------
    # Detail-page fields (populated when scrape_zoopla_agency fetches
    # each listing's /details/ page — opt-in via ?details=true)
    # ------------------------------------------------------------------
    furnished_state: Optional[str] = None           # canonical from hidden JSON:
                                                    #   furnished / unfurnished / part_furnished /
                                                    #   semi_furnished / furnished_or_unfurnished
    furnished_label: Optional[str] = None           # visible DOM text: "Furnished", "Unfurnished" etc.
    epc_rating: Optional[str] = None                # single letter A–G (badge + text)
    available_date: Optional[str] = None            # raw text extracted from page
    features: List[str] = Field(default_factory=list)  # bullet list from "About this property"
    description: Optional[str] = None               # full marketing description
    size_sq_ft: Optional[int] = None                # extracted from description text
    deposit: Optional[int] = None                   # holding/security deposit in £
    council_tax_band: Optional[str] = None          # A–J, "Not yet known", "Tbc" etc.
    parking: bool = False                           # True if any feature/description mentions parking
    outdoor_space: bool = False                     # True if garden / patio / yard / driveway mentioned
    bills_included: bool = False                    # True if "bills inclusive" / "bills included" found
    agent_name: Optional[str] = None                # branch/brand name from hidden JSON
    num_photos: Optional[int] = None                # from hidden JSON (num_images)
    has_floorplan: bool = False                     # from hidden JSON (has_floorplan)
    is_shared_ownership: bool = False               # from hidden JSON
    is_retirement_home: bool = False                # from hidden JSON
    tenure: Optional[str] = None                    # from hidden JSON
    listing_condition: Optional[str] = None         # from hidden JSON: pre-owned / new / etc.
    property_type_detail: Optional[str] = None      # canonical from hidden JSON (flat, teraced, etc.)


class AgencyListingsResponse(BaseModel):
    agency: str
    listing_type: str
    properties: List[Property]
    total: int
    cached: bool = False


def _to_int(text: Optional[str]) -> Optional[int]:
    if text is None:
        return None
    if isinstance(text, int):
        return text
    if isinstance(text, float):
        return int(text)
    s = str(text).strip()
    if not s:
        return None
    digits = re.sub(r"[^\d]", "", s)
    return int(digits) if digits else None


def _extract_listing_id(url: str) -> Optional[str]:
    m = LISTING_ID_RE.search(url or "")
    return m.group(1) if m else None


# ============================================================================
# Detail-page extraction helpers
# ============================================================================

def _extract_hidden_json(html_content: str) -> dict:
    """Extract Zoopla's hidden ListingAnalyticsTaxonomy JSON from the page.

    Zoopla embeds this as a standalone JSON object (no wrapping script tag
    with a type attribute) inside the HTML.  We hunt for the JSON object
    that starts with {"__typename":"ListingAnalyticsTaxonomy".
    """
    low = html_content
    start_marker = '{"__typename":"ListingAnalyticsTaxonomy"'
    idx = low.find(start_marker)
    if idx < 0:
        return {}

    # Walk forward to find the matching close brace.
    depth = 0
    in_string = False
    escape_next = False
    end = idx
    for i in range(idx, len(low)):
        ch = low[i]
        if escape_next:
            escape_next = False
            continue
        if ch == "\\" and in_string:
            escape_next = True
            continue
        if ch == '"' and not escape_next:
            in_string = not in_string
            continue
        if not in_string:
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break

    candidate = low[idx:end]
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        logger.debug("Could not parse hidden JSON from detail page (truncated candidate: %s...)", candidate[:200])
        return {}


def _extract_features_and_description(page_selector: Selector) -> tuple:
    """Extract the bullet-feature list and the full description from a detail page.

    Zoopla's detail pages structure the description as:
      - Collapsed teaser (first paragraph under "About this property")
      - Full description behind a "Read full description" expandable, or
        the full text visible right after the features bullets.
    """
    features: List[str] = []
    description: Optional[str] = None

    # Try to find the "About this property" section and its children.
    # NOTE: scrapling's Selector (lxml-backed) does NOT support Playwright's
    # :has-text() pseudo-class, so we match candidate nodes with plain CSS and
    # then filter by visible text in Python.
    about_node = page_selector.css(
        '[class*="aboutThisProperty"], [class*="propertyDescription"], '
        'h2, [class*="description"]',
        auto_save=False,
    )

    # Strategy 0: narrow the candidate set to the node actually headed
    # "About this property" (h2 + its container), preferring an exact match.
    about = None
    for node in about_node:
        txt = (node.text or "")
        if "about this property" in txt.lower():
            about = node
            # climb to the container that holds the bullets/description
            parent = node.parent
            if parent is not None:
                about = parent
            break
    if about is None and about_node:
        about = about_node[0]

    # Strategy 1: grab all <li> / bullet elements inside or near the about section
    if about is not None:
        # Features are typically <li> elements or div bullets
        feature_nodes = about.css("li, [class*='feature'], [class*='bullet']", auto_save=False)
        for fn in feature_nodes:
            txt = (fn.text or "").strip()
            if txt and len(txt) < 200 and txt not in features:
                features.append(txt)
        if not features:
            # Fallback: try immediate text children as bullet items.
            # Skip the section heading itself (e.g. "About this property").
            for child in about.children:
                txt = (child.text or "").strip()
                if txt and len(txt) < 200 and txt not in features and len(features) < 30:
                    if "about this property" not in txt.lower():
                        features.append(txt)

    # Strategy 2: if no features found in about section, look for any list on the page
    # that's in the property description area
    if not features:
        full_desc_blocks = page_selector.css(
            '[class*="description"], [class*="propertyDesc"], '
            '[data-testid*="description"], article p',
            auto_save=False,
        )
        for block in full_desc_blocks:
            txt = (block.text or "").strip()
            if txt and len(txt) > 100 and len(txt) < 5000:
                description = txt
                # Try splitting on bullet markers commonly used in Zoopla descriptions
                for bullet in re.split(r"\n+|•|\*|●|\u2022", txt):
                    b = bullet.strip()
                    if b and len(b) < 200 and len(features) < 30:
                        features.append(b)
                break

    # If we still have no description, grab the largest text block from the page
    # that looks like a property description (long paragraph with property terms).
    if not description:
        all_text_blocks = page_selector.css("p, div", auto_save=False)
        best: Optional[str] = None
        best_len = 0
        for block in all_text_blocks:
            txt = (block.text or "").strip()
            if 200 < len(txt) < 6000 and len(txt) > best_len:
                # Heuristic: description mentions property terms
                if re.search(r"property|bedroom|kitchen|bathroom|living|accommodation|let|rent|floor|room", txt, re.I):
                    best = txt
                    best_len = len(txt)
        description = best

    # Deduplicate features while preserving order
    seen_feat = set()
    deduped = []
    for f in features:
        lf = f.lower()
        if lf not in seen_feat and len(f.strip()) > 1:
            seen_feat.add(lf)
            deduped.append(f.strip())
    features = deduped[:40]  # cap at 40 features

    return features, description


def _extract_bool_flag(text: str, pattern: re.Pattern, default: bool = False) -> bool:
    """Return True if `pattern` matches `text` (case-insensitive)."""
    return bool(pattern.search(text))


def _parse_detail_page(html_content: str, listing_url: str) -> dict:
    """Parse a Zoopla property detail page and return a dict of extra fields.

    Extracts:
      - Hidden JSON (ListingAnalyticsTaxonomy) for canonical structured fields
      - Visible DOM text for EPC, furnished label, available date, etc.
      - Full description and feature bullet list
      - Boolean flags inferred from features + description text
    """
    result: dict = {}

    page_selector = Selector(html_content, adaptive=True, url=listing_url)

    # ------------------------------------------------------------------
    # 1. Hidden JSON (structured canonical data)
    # ------------------------------------------------------------------
    hidden = _extract_hidden_json(html_content)
    if hidden:
        result["furnished_state"] = hidden.get("furnished_state")
        result["property_type_detail"] = hidden.get("property_type")
        result["size_sq_ft"] = _to_int(hidden.get("size_sq_feet"))
        result["has_floorplan"] = bool(hidden.get("has_floorplan") in (True, "true", "1"))
        result["is_shared_ownership"] = bool(hidden.get("is_shared_ownership") in (True, "true", "1"))
        result["is_retirement_home"] = bool(hidden.get("is_retirement_home") in (True, "true", "1"))
        result["listing_condition"] = hidden.get("listing_condition")
        result["tenure"] = hidden.get("tenure")
        result["agent_name"] = hidden.get("branch_name") or hidden.get("brand_name")
        result["num_photos"] = _to_int(hidden.get("num_images"))

    # ------------------------------------------------------------------
    # 2. Visible DOM: EPC, furnished label, available date, description
    # ------------------------------------------------------------------
    full_text = (page_selector.css("body", auto_save=False) or [None])[0]
    body_text = (full_text.text if full_text else "").strip()

    # EPC rating: look for "EPC Rating: C" pattern
    epc_m = EPC_RE.search(body_text)
    if epc_m:
        result["epc_rating"] = epc_m.group(1).upper()

    # Furnished label: search for the exact label text near the top of the page
    # (Zoopla renders it as a label under the EPC rating line)
    for line in body_text.splitlines():
        line = line.strip()
        fm = FURNISHED_LABEL_RE.match(line)
        if fm:
            result["furnished_label"] = fm.group(1).strip()
            # Normalise to lower-case canonical form, but keep the display label
            break

    # Available date: search the full text for "Available from ..." patterns
    avail_m = AVAILABLE_RE.search(body_text)
    if avail_m:
        result["available_date"] = avail_m.group(0).strip()

    # Features + description
    features, description = _extract_features_and_description(page_selector)
    result["features"] = features
    result["description"] = description

    # ------------------------------------------------------------------
    # 3. Boolean flags inferred from features + description
    # ------------------------------------------------------------------
    combined_text = " ".join(features) + " " + (description or "")

    # Parking
    result["parking"] = _extract_bool_flag(combined_text, PARKING_KEYWORDS)
    # Outdoor space
    result["outdoor_space"] = _extract_bool_flag(combined_text, OUTDOOR_KEYWORDS)
    # Bills included
    result["bills_included"] = _extract_bool_flag(combined_text, BILLS_KEYWORDS)

    # ------------------------------------------------------------------
    # 4. Deposit and council tax from "More information" section
    # ------------------------------------------------------------------
    # Deposit: try both "Deposit: £X" and "Holding Deposit: £X"
    dep_m = DEPOSIT_RE.search(body_text)
    if dep_m:
        result["deposit"] = _to_int(dep_m.group(1))
    # Holding deposit overrides the general deposit if present
    hd_m = HOLDING_DEPOSIT_RE.search(body_text)
    if hd_m and not result.get("deposit"):
        result["deposit"] = _to_int(hd_m.group(1))

    # Council tax band
    ct_m = COUNCIL_TAX_RE.search(body_text)
    if ct_m:
        band = ct_m.group(1).strip()
        # Normalise "Tbc" / "To be confirmed" to "Not yet known"
        if band.lower() in ("tbc", "tbc.", "to be confirmed", "to be confirmed."):
            band = "Not yet known"
        result["council_tax_band"] = band

    return result


def _enrich_property(prop: Property, detail_fields: dict) -> Property:
    """Apply extracted detail-fields to a Property, preserving existing values."""
    update_data: dict = {}
    for key, value in detail_fields.items():
        # Only set if the property doesn't already have a value for this field
        current = getattr(prop, key, None)
        if current is None or current == ( False if isinstance(current, bool) else "" ):
            update_data[key] = value
    if update_data:
        prop = prop.model_copy(update=update_data)
    return prop


# ============================================================================
# Scraper
# ============================================================================

def scrape_zoopla_agency(branch_id: str, max_pages: int = 3, listing_type: str = "rent",
                          fetch_details: bool = True) -> List[Property]:
    """Scrape Zoopla agency listing cards, optionally fetching each detail page.

    Args:
        branch_id:      Zoopla branch ID.
        max_pages:      Maximum search-result pages to crawl (1–10).
        listing_type:   "rent" or "sale".
        fetch_details:  If True, after collecting card data, visit each unique
                        listing's /details/ page and enrich the Property with
                        furnished state, EPC, features, description, flags etc.
    """
    properties: List[Property] = []
    start = time.time()
    listing_type = listing_type.lower()
    if listing_type not in ("rent", "sale"):
        listing_type = "rent"
    section = "for-sale" if listing_type == "sale" else "to-rent"
    base_url = f"https://www.zoopla.co.uk/{section}/property/uk/?branch_id={branch_id}&include_sold=true&include_rented=true"

    # Use the warm browser pool for every fetch.
    page = 1
    seen_listing_ids: set = set()  # dedupe across pages in case of overlap

    # First pass: collect cards (listing_id → Property at card level)
    cards: dict = {}  # listing_id → Property

    while page <= max_pages:
        url = f"{base_url}&pn={page}" if page > 1 else base_url
        logger.info("Scraping page %s: %s", page, url)
        try:
            response = fetch_via_browser(
                url,
                wait_selector='[data-testid="listing-card-content"]',
                wait_selector_state="attached",
                timeout=60000,
                network_idle=True,
                solve_cloudflare=True,
                selector_config=_STEALTH_PARSER_ARGS,
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
                prop = parse_listing(row, listing_type, cards)
                if prop and prop.listing_id and prop.listing_id not in cards:
                    cards[prop.listing_id] = prop
                    page_count += 1
                elif prop:
                    # Include it but log the dedup
                    logger.debug("Page %s: duplicate listing_id %s, skipping", page, prop.listing_id)
                    page_count += 1
            logger.info("Page %s: extracted %s properties (new)", page, page_count)
            if page_count == 0:
                break

            # --- Check whether a next page actually exists before fetching it ---
            has_next = _has_next_page(page_selector, page)
            if not has_next:
                logger.info("Page %s: no next-page link found — reached end of results", page)
                break

            page += 1
            time.sleep(1)
        except Exception as e:
            logger.error("Error on page %s: %s", page, e)
            break

    # Convert card dict to list
    if cards:
        properties = list(cards.values())

    # Second pass: fetch detail pages for each unique listing (if requested).
    # Run them CONCURRENTLY across the browser pool so N listings take ~1 fetch
    # instead of N sequential ones (keeps the whole scrape well under 5s).
    if fetch_details and properties:
        logger.info("Fetching detail pages for %s listings (parallel)...", len(properties))

        def _enrich_one(i: int, prop: Property) -> tuple:
            if not prop.listing_url:
                logger.debug("Listing %s has no URL, skipping detail fetch", prop.listing_id)
                return i, None
            try:
                detail_fields = _scrape_property_details(prop.listing_url)
                if detail_fields:
                    return i, _enrich_property(prop, detail_fields)
            except Exception as e:
                logger.warning("Failed to fetch details for %s (%s): %s", prop.listing_id, prop.listing_url, e)
            return i, None

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(len(properties), max(1, BROWSER_WORKERS))
        ) as detail_pool:
            futures = [detail_pool.submit(_enrich_one, i, prop) for i, prop in enumerate(properties)]
            for fut in concurrent.futures.as_completed(futures):
                i, enriched = fut.result()
                if enriched is not None:
                    properties[i] = enriched

        logger.info("Detail fetch complete: %s/%s listings enriched",
                     sum(1 for p in properties if p.description), len(properties))

        elapsed = time.time() - start
        logger.info("Scrape complete in %.2fs (%s listings, details=%s)",
                    elapsed, len(properties), bool(fetch_details))
        return properties


def _scrape_property_details(listing_url: str) -> dict:
    """Fetch and parse a single Zoopla property detail page.

    Returns a dict of extra fields suitable for passing to _enrich_property,
    or an empty dict on failure. Uses the warm browser pool (a worker is
    picked round-robin, so many concurrent calls fan out across browsers).
    """
    try:
        response = fetch_via_browser(
            listing_url,
            wait_selector='[data-testid="listing-card-content"], article, main',
            wait_selector_state="attached",
            timeout=30000,
            network_idle=True,
            solve_cloudflare=True,
            selector_config=_STEALTH_PARSER_ARGS,
        )
        if response.status >= 400:
            logger.warning("Detail page fetch failed for %s: status %s", listing_url, response.status)
            return {}
        html_content = response.text or ""
        if not html_content and response.body:
            html_content = response.body.decode("utf-8", "ignore") if isinstance(response.body, bytes) else str(response.body)
        if not html_content:
            return {}
        if len(html_content) < 2000:
            logger.warning("Suspiciously short detail page for %s (%s bytes)", listing_url, len(html_content))
            return {}

        # Challenge detection
        low = html_content.lower()
        if any(kw in low for kw in (
            "are you a robot", "verify you are human", "checking your browser",
            "just a moment", "cf-chl", "please enable javascript",
        )):
            logger.warning("Bot challenge on detail page: %s", listing_url)
            return {}

        return _parse_detail_page(html_content, listing_url)
    except Exception as e:
        logger.error("Exception scraping detail page %s: %s", listing_url, e)
        return {}


def _has_next_page(selector: Selector, current_page: int) -> bool:
    """Return True if Zoopla's pagination shows a next-page link/button.

    Zoopla renders a pager with page-number links and a 'Next' control.
    We check for a next-page indicator using several common selector shapes:
    - an anchor whose text/content signals 'Next'
    - a page-number link for page (current_page + 1)
    - a rel='next' link
    """
    # 1. Look for a "next" link by rel attribute or common text.
    #    CSS :contains() and :has() aren't valid CSS — scrapling rejects them —
    #    so we match by attributes and then filter by text content.
    next_link = selector.css('a[rel="next"], a[href*="pn="][class*="next"], a[class*="next-page"]', auto_save=False)
    if not next_link:
        next_link = selector.css('a[rel="next"], a[href*="pn="]', auto_save=False)

    if next_link:
        for link in next_link:
            href = link.attrib.get("href", "")
            # If the href carries a pn= param, it must point to the expected next page.
            if "pn=" in href:
                if f"pn={current_page + 1}" in href:
                    return True
                # pn= present but wrong page — not a valid next link
                continue
            # No pn= in href: trust text/rel-based cues
            txt = (link.text or "").strip().lower()
            if txt in ("next", "next page", "›", ">") or "next" in txt:
                return True
            rel = link.attrib.get("rel", "").lower()
            if rel == "next":
                return True

    # 2. Look for a page-number link for current_page + 1
    next_page_links = selector.css(f'a[href*="pn={current_page + 1}"], a[data-page="{current_page + 1}"]', auto_save=False)
    if next_page_links:
        return True

    # 3. Fallback: check if the pager container has a "next" class element
    pager_next = selector.css(
        '[class*="pager"] [class*="next"], [class*="pagination"] [class*="next"], '
        '[class*="paging"] [class*="next"]',
        auto_save=False,
    )
    if pager_next:
        return True

    return False


def _first(node: Selector, selector: str, identifier: str, auto_save: bool):
    found = node.css(selector, auto_save=auto_save, identifier=identifier)
    if not found:
        found = node.css(selector, adaptive=True, identifier=identifier)
    return found[0] if found else None


def parse_listing(row: Selector, listing_type: str, cards: dict = None) -> Optional[Property]:
    """Parse a single listing card row into a Property (card-level data only)."""
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
    """Serialize a list of Properties to CSV, including all detail-page fields."""
    fields = [
        "listing_id", "title", "price", "price_pcm", "price_per_week", "address",
        "bedrooms", "bathrooms", "property_type", "listing_type", "listing_url", "image_url",
        # detail-page fields
        "furnished_state", "furnished_label", "epc_rating", "available_date",
        "features", "description", "size_sq_ft", "deposit", "council_tax_band",
        "parking", "outdoor_space", "bills_included", "agent_name", "num_photos",
        "has_floorplan", "is_shared_ownership", "is_retirement_home", "tenure",
        "listing_condition", "property_type_detail",
    ]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for p in properties:
        row = p.model_dump()
        # Join list fields (features) into a pipe-separated string for CSV
        if "features" in row and row["features"]:
            row["features"] = "|".join(row["features"])
        writer.writerow(row)
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
    details: bool = Query(False, description="If true, also fetch each listing's detail page in this single request (can exceed Cloudflare's 125s limit for large agencies). Default false returns cards only — pair with /api/property/{listing_id} for lazy per-listing enrichment."),
    record: dict = Depends(authenticate),
):
    # 1. Rate limit (per owner)
    limit = record["rate_limit"] or DEFAULT_RATE_LIMIT
    if not limiter.allow(record["owner"], limit):
        raise HTTPException(status_code=429, detail=f"Rate limit exceeded ({limit}/min for owner '{record['owner']}')")

    cache_key = (branch_id, listing_type, max_pages, details)
    cached = cache.get(cache_key)
    if cached is not None:
        if fmt == "csv":
            return PlainTextResponse(properties_to_csv(cached), media_type="text/csv",
                                     headers={"Content-Disposition": f'attachment; filename="agency_{branch_id}_{listing_type}.csv"'})
        return AgencyListingsResponse(agency=branch_id, listing_type=listing_type, properties=cached, total=len(cached), cached=True)

    loop = asyncio.get_event_loop()
    with concurrent.futures.ThreadPoolExecutor() as pool:
        properties = await loop.run_in_executor(pool, scrape_zoopla_agency, branch_id, max_pages, listing_type, details)

    # Never cache empty results: they are usually a transient bot-challenge/empty
    # page, and caching them would poison the cache for the whole TTL window.
    if properties:
        cache.set(cache_key, properties)
    if fmt == "csv":
        return PlainTextResponse(properties_to_csv(properties), media_type="text/csv",
                                 headers={"Content-Disposition": f'attachment; filename="agency_{branch_id}_{listing_type}.csv"'})
    return AgencyListingsResponse(agency=branch_id, listing_type=listing_type, properties=properties, total=len(properties), cached=False)


@app.get("/api/property/{listing_id}")
async def get_property_details(
    listing_id: str,
    fmt: str = Query("json", description="json or csv"),
    record: dict = Depends(authenticate),
):
    """Fetch enriched detail data for a single Zoopla listing.

    Designed as the lazy/per-listing half of the two-trigger workflow so each
    HTTP request stays well under Cloudflare's 125s limit:

      1. /api/agency/{branch_id}            -> cards only (fast)
      2. /api/property/{listing_id}         -> details for one listing (fast)

    `listing_id` is the numeric ID from a card's `listing_url`
    (.../details/<id>/). It is turned into the canonical Zoopla details URL,
    which is also returned in the response so callers can self-correct.
    """
    # 1. Rate limit (per owner)
    limit = record["rate_limit"] or DEFAULT_RATE_LIMIT
    if not limiter.allow(record["owner"], limit):
        raise HTTPException(status_code=429, detail=f"Rate limit exceeded ({limit}/min for owner '{record['owner']}')")

    listing_id = listing_id.strip()
    if not listing_id.isdigit():
        raise HTTPException(status_code=400, detail="listing_id must be the numeric Zoopla listing ID (digits only)")

    listing_url = f"https://www.zoopla.co.uk/details/{listing_id}/"

    cache_key = ("property", listing_id)
    cached = cache.get(cache_key)
    if cached is not None:
        prop = cached
        cached_flag = True
    else:
        loop = asyncio.get_event_loop()
        # Reuse the pooled browser detail fetch. Returns a dict of extra fields
        # (or {} on failure), so wrap it into a Property built from the URL.
        with concurrent.futures.ThreadPoolExecutor() as pool:
            detail_fields = await loop.run_in_executor(pool, _scrape_property_details, listing_url)
        if not detail_fields:
            # Surface a 502 so app-script triggers can detect + retry cleanly
            # (transient Cloudflare challenge / empty body).
            raise HTTPException(status_code=502, detail="Failed to scrape detail page (likely bot-challenge or empty body)")
        # Keep only keys that are valid Property fields (tolerate schema drift
        # in the extractor without 500-ing the endpoint).
        valid = {k: v for k, v in detail_fields.items() if k in Property.model_fields}
        prop = Property(
            listing_id=listing_id,
            title="",
            price="",
            address="",
            listing_type=None,
            listing_url=listing_url,
            **valid,
        )
        cache.set(cache_key, prop)
        cached_flag = False

    if fmt == "csv":
        return PlainTextResponse(
            properties_to_csv([prop]), media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="property_{listing_id}.csv"'},
        )
    return prop.model_dump()


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
