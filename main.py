from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from typing import Optional, List
import asyncio
import csv
import io
import logging
import re
import time

from scrapling.fetchers import StealthyFetcher
from scrapling import Selector

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Mini Zoopla API", version="1.1.0")

# Enable adaptive mode globally
StealthyFetcher.adaptive = True

# Adaptive selectors derived by inspecting the real rendered DOM of
# https://www.zoopla.co.uk/to-rent/property/uk/?branch_id=XXXX
# (and the equivalent /for-sale/ page). They auto-save on first successful
# run; on later runs adaptive=True heals layout changes.
ADAPTIVE_CONFIG = {
    "listing_row": {
        "selector": '[id^="listing_"]',
        "identifier": "zoopla_listing_row",
        "auto_save": True,
    },
    "price": {
        "selector": '[data-testid="listing-card-content"] p[class*="priceText"]',
        "identifier": "zoopla_listing_price",
        "auto_save": True,
    },
    "amenities": {
        "selector": 'p[class*="amenityListSlim"] span[class*="amenityItemSlim"]',
        "identifier": "zoopla_listing_amenities",
        "auto_save": True,
    },
    "address": {
        "selector": 'address[class*="summary_address"]',
        "identifier": "zoopla_listing_address",
        "auto_save": True,
    },
    "title": {
        "selector": '[data-testid="listing-card-content"] p[class*="summary"]',
        "identifier": "zoopla_listing_title",
        "auto_save": True,
    },
    "link": {
        "selector": 'a[class*="detailsPageLink"]',
        "identifier": "zoopla_listing_link",
        "auto_save": True,
    },
    "image": {
        "selector": 'a[class*="galleryLink"] img',
        "identifier": "zoopla_listing_image",
        "auto_save": True,
    },
}

# CSS classes (hashed) observed in the saved HTML — used as stable fallbacks
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


def _to_int(text: Optional[str]) -> Optional[int]:
    if not text:
        return None
    digits = re.sub(r"[^\d]", "", text)
    return int(digits) if digits else None


def _extract_listing_id(url: str) -> Optional[str]:
    m = LISTING_ID_RE.search(url or "")
    return m.group(1) if m else None


def scrape_zoopla_agency(
    agency_slug: str, max_pages: int = 3, listing_type: str = "rent"
) -> List[Property]:
    """Scrape Zoopla agency listings using scrapling with adaptive selectors.

    agency_slug is the branch id, e.g. "12345".
    listing_type is "rent" or "sale".
    """
    properties: List[Property] = []
    listing_type = listing_type.lower()
    if listing_type not in ("rent", "sale"):
        listing_type = "rent"
    section = "for-sale" if listing_type == "sale" else "to-rent"
    base_url = f"https://www.zoopla.co.uk/{section}/property/uk/?branch_id={agency_slug}"

    fetcher = StealthyFetcher()
    for page in range(1, max_pages + 1):
        url = f"{base_url}&pn={page}" if page > 1 else base_url
        logger.info("Scraping page %s: %s", page, url)
        try:
            response = fetcher.fetch(
                url,
                wait_selector='[data-testid="listing-card-content"]',
                wait_selector_state="attached",
                timeout=60000,
                headless=True,
                network_idle=True,
            )
            if response.status >= 400:
                logger.warning("Fetch failed page %s: %s", page, response.status)
                break

            html_content = response.text or ""
            if not html_content and response.body:
                html_content = response.body.decode("utf-8", "ignore") if isinstance(
                    response.body, bytes) else str(response.body)
            if not html_content:
                logger.warning("Empty response body on page %s", page)
                break

            page_selector = Selector(html_content, adaptive=True, url="zoopla.co.uk")
            rows = page_selector.css(
                ADAPTIVE_CONFIG["listing_row"]["selector"],
                auto_save=ADAPTIVE_CONFIG["listing_row"]["auto_save"],
                identifier=ADAPTIVE_CONFIG["listing_row"]["identifier"],
            )
            if not rows:
                rows = page_selector.css(
                    ADAPTIVE_CONFIG["listing_row"]["selector"],
                    adaptive=True,
                    identifier=ADAPTIVE_CONFIG["listing_row"]["identifier"],
                )
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
    """Helper: return first matching Selector child or None (with adaptive fallback)."""
    found = node.css(selector, auto_save=auto_save, identifier=identifier)
    if not found:
        found = node.css(selector, adaptive=True, identifier=identifier)
    return found[0] if found else None


def parse_listing(row: Selector, listing_type: str) -> Optional[Property]:
    try:
        # Price
        price_elem = _first(row, ADAPTIVE_CONFIG["price"]["selector"],
                            ADAPTIVE_CONFIG["price"]["identifier"],
                            ADAPTIVE_CONFIG["price"]["auto_save"])
        price_text = price_elem.text.strip() if price_elem is not None else ""
        price_pcm = None
        price_per_week = None
        m_price = PRICE_RE.search(price_text)
        if m_price:
            price_pcm = _to_int(m_price.group(1))
        # Sale prices have no pcm/pw; keep pcm as the asking price for sale
        if listing_type == "sale" and price_pcm is None:
            sale_m = re.search(r"£([\d,]+)", price_text)
            if sale_m:
                price_pcm = _to_int(sale_m.group(1))
        if price_elem is not None and price_elem.parent is not None:
            alt = _first(price_elem.parent, '[class*="priceAlternative"]',
                         "zoopla_listing_price_alt", True)
            if alt is not None:
                m_alt = PRICE_RE.search(alt.text)
                if m_alt and m_alt.group(2).lower() == "pw":
                    price_per_week = _to_int(m_alt.group(1))

        # Amenities (beds/baths)
        amenities = row.css(ADAPTIVE_CONFIG["amenities"]["selector"],
                            auto_save=ADAPTIVE_CONFIG["amenities"]["auto_save"],
                            identifier=ADAPTIVE_CONFIG["amenities"]["identifier"])
        if not amenities:
            amenities = row.css(ADAPTIVE_CONFIG["amenities"]["selector"],
                                adaptive=True,
                                identifier=ADAPTIVE_CONFIG["amenities"]["identifier"])
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

        # Address
        addr_elem = _first(row, ADAPTIVE_CONFIG["address"]["selector"],
                           ADAPTIVE_CONFIG["address"]["identifier"],
                           ADAPTIVE_CONFIG["address"]["auto_save"])
        address = addr_elem.text.strip() if addr_elem is not None else ""

        # Title (description paragraph right after the address)
        title = address
        if addr_elem is not None and addr_elem.next is not None:
            title = addr_elem.next.text.strip() or address

        # Link
        link_elem = _first(row, ADAPTIVE_CONFIG["link"]["selector"],
                           ADAPTIVE_CONFIG["link"]["identifier"],
                           ADAPTIVE_CONFIG["link"]["auto_save"])
        href = link_elem.attrib.get("href") if link_elem is not None else None
        listing_url = f"https://www.zoopla.co.uk{href}" if href and href.startswith("/") else (href or "")
        listing_id = _extract_listing_id(listing_url)

        # Image (skip data: URIs from SingleFile saves)
        img_elem = _first(row, ADAPTIVE_CONFIG["image"]["selector"],
                          ADAPTIVE_CONFIG["image"]["identifier"],
                          ADAPTIVE_CONFIG["image"]["auto_save"])
        image_url = None
        if img_elem is not None:
            src = img_elem.attrib.get("src")
            if src and src.startswith("http"):
                image_url = src

        return Property(
            listing_id=listing_id,
            title=title or "Unknown",
            price=price_text or "POA",
            price_pcm=price_pcm,
            price_per_week=price_per_week,
            address=address,
            bedrooms=bedrooms,
            bathrooms=bathrooms,
            property_type=None,
            listing_type=listing_type,
            listing_url=listing_url,
            image_url=image_url,
        )
    except Exception as e:
        logger.debug("Parse error: %s", e)
        return None


def properties_to_csv(properties: List[Property]) -> str:
    fields = [
        "listing_id", "title", "price", "price_pcm", "price_per_week",
        "address", "bedrooms", "bathrooms", "property_type", "listing_type",
        "listing_url", "image_url",
    ]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for p in properties:
        writer.writerow(p.model_dump())
    return buf.getvalue()


@app.get("/health")
async def health_check():
    return {"status": "healthy"}


@app.get("/api/agency/{agency_slug}")
async def get_agency_listings(
    agency_slug: str,
    max_pages: int = Query(3, ge=1, le=10, description="Maximum pages to scrape"),
    listing_type: str = Query("rent", description="rent or sale"),
    fmt: str = Query("json", description="json or csv"),
):
    """Get all listings for a specific Zoopla agency branch using adaptive scraping."""
    listing_type = listing_type.lower()
    if listing_type not in ("rent", "sale"):
        raise HTTPException(status_code=400, detail="listing_type must be 'rent' or 'sale'")
    if fmt not in ("json", "csv"):
        raise HTTPException(status_code=400, detail="fmt must be 'json' or 'csv'")

    loop = asyncio.get_event_loop()
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor() as pool:
        properties = await loop.run_in_executor(
            pool, scrape_zoopla_agency, agency_slug, max_pages, listing_type
        )

    if fmt == "csv":
        csv_text = properties_to_csv(properties)
        return PlainTextResponse(csv_text, media_type="text/csv",
                                 headers={"Content-Disposition":
                                           f'attachment; filename="agency_{agency_slug}_{listing_type}.csv"'})
    return AgencyListingsResponse(
        agency=agency_slug,
        listing_type=listing_type,
        properties=properties,
        total=len(properties),
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
