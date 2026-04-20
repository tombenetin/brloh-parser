import json
import os
import re
import sqlite3
import sys
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import requests
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

APP_HOST = "0.0.0.0"
APP_PORT = int(os.getenv("APP_PORT", "8093"))
APP_VERSION = "0.2.0"

AGGREGATOR_SYNC_ENABLED = os.getenv("AGGREGATOR_SYNC_ENABLED", "1") == "1"
AGGREGATOR_BASE = os.getenv("AGGREGATOR_BASE", "http://127.0.0.1:8092").rstrip("/")
AGGREGATOR_INGEST_PATH = os.getenv("AGGREGATOR_INGEST_PATH", "/api/ingest/run")

SHOP = "brloh"
SOURCE_URLS = [
    "https://www.brloh.sk/Vyhladavanie/pokemon-karty?query=tcg#s=r&st=1",
]
SOURCE_URL = SOURCE_URLS[0]

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0.0.0 Safari/537.36"
)

DB_PATH = Path(os.getenv("DB_PATH", "/home/brloh-parser/brloh.db"))
CHANGELOG_PATH = Path("CHANGELOG.md")

EVENT_TYPE_NEW_PRODUCT = "new_product"
EVENT_TYPE_PRICE_CHANGE = "price_change"
EVENT_TYPE_AVAILABILITY_CHANGE = "availability_change"
EVENT_TYPE_PRODUCT_DISAPPEARED = "product_disappeared"
EVENT_TYPE_SCAN_ERROR = "scan_error"

PRODUCT_KEYWORDS = [
    "pokemon",
    "pokémon",
    "tcg",
    "booster",
    "blister",
    "elite trainer",
    "etb",
    "tin",
    "collection",
    "binder",
    "mini tin",
    "trainer box",
    "premium collection",
    "booster box",
    "bundle",
    "deck",
    "album na karty",
    "albumy na karty",
    "pokemon company",
]

BANNED_TITLE_EXACT = {
    "Prejsť na obsah",
    "Pokračovať do košíka",
    "Kontaktné údaje",
    "Obchodné podmienky",
    "GDPR",
    "CZK",
    "EUR",
    "Slovenčina",
    "English",
    "Prihlásenie",
    "Pokémon Karty",
    "Pokémon karty",
    "Zberateľské karty",
    "Všetky filtre",
}

app = FastAPI(title="brloh-parser", version=APP_VERSION)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def normalize_ws(value: Optional[str]) -> str:
    return re.sub(r"\s+", " ", (value or "")).replace("\xa0", " ").strip()


def db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def price_to_float(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    s = str(value)
    s = s.replace("€", "").replace(" ", "").replace("\xa0", "").replace(",", ".").strip()
    s = re.sub(r"^od", "", s, flags=re.IGNORECASE).strip()
    try:
        return float(s)
    except Exception:
        return None


def normalize_price(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    raw = normalize_ws(value).replace("EUR", "€").strip()
    n = price_to_float(raw)
    if n is None:
        return raw
    is_from = raw.lower().startswith("od")
    out = (f"od €{n:.2f}" if is_from else f"€{n:.2f}").replace(".", ",")
    return out.replace(",00", "")


def normalize_availability(value: Optional[str]) -> str:
    s = normalize_ws(value).lower()
    if not s:
        return ""

    if "predaj ukončený" in s:
        return "predaj ukončený"
    if "vypredané" in s:
        return "vypredané"
    if "predobjednávka" in s:
        qty = re.search(r"\(([^)]+)\)", s)
        return f"predobjednávka ({normalize_ws(qty.group(1)).lower()})" if qty else "predobjednávka"
    if "na objednávku" in s:
        return "na objednávku"
    if "nedostup" in s:
        return "nedostupné"
    if "neznáma dostupnosť" in s:
        return "neznáma dostupnosť"
    if "posledných" in s:
        return normalize_ws(s)
    if "na sklade" in s:
        qty = re.search(r"na sklade\s*([><]?\s*\d+\s*ks)", s, flags=re.IGNORECASE)
        if qty:
            return f"na sklade {normalize_ws(qty.group(1)).lower()}"
        return "na sklade"
    if "skladom" in s:
        qty = re.search(r"\(([^)]+)\)", s)
        return f"skladom ({normalize_ws(qty.group(1)).lower()})" if qty else "skladom"

    return s


def is_available_now(availability: Optional[str]) -> bool:
    text = normalize_availability(availability)
    if not text:
        return False

    negative_patterns = [
        r"\bvypredan",
        r"\bnedostupn",
        r"\bpredaj ukončený\b",
        r"\bneznáma dostupnosť\b",
        r"\bna objednávku\b",
        r"\bpredobjednávka\b",
        r"\bout\s+of\s+stock\b",
        r"\bsold\s*out\b",
    ]
    for pat in negative_patterns:
        if re.search(pat, text, flags=re.IGNORECASE):
            return False

    positive_patterns = [
        r"\bna sklade\b",
        r"\bskladom\b",
        r"\bposledných\b",
        r">\s*\d+\s*ks",
        r"\bin stock\b",
    ]
    for pat in positive_patterns:
        if re.search(pat, text, flags=re.IGNORECASE):
            return True

    return False


def slug_from_url(url: str) -> str:
    path = urlparse(url).path.strip("/")
    m = re.search(r"-p(\d+)", path, flags=re.IGNORECASE)
    if m:
        return m.group(1)
    return path.split("/")[-1].strip().lower() if path else ""


def is_allowed_product_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    if parsed.netloc != "www.brloh.sk":
        return False
    if not re.search(r"-p\d+/?$", parsed.path or "", flags=re.IGNORECASE):
        return False
    return True


def contains_product_keywords(*values: Optional[str]) -> bool:
    haystack = " ".join(normalize_ws(v).lower() for v in values if v)
    if not haystack:
        return False
    return any(keyword in haystack for keyword in PRODUCT_KEYWORDS)


def looks_like_real_product(
    title: str,
    url: str,
    price: Optional[str],
    availability: Optional[str],
    raw_text: Optional[str],
) -> bool:
    t = normalize_ws(title)
    if not t or t in BANNED_TITLE_EXACT or len(t) < 4:
        return False
    if not is_allowed_product_url(url):
        return False
    if not price and not availability:
        return False

    bad_fragments = [
        "Do košíka",
        "Detail",
        "Hľadať",
        "Prázdny košík",
        "Zobraziť viac produktov",
        "Odporúčame",
        "Najlacnejšie",
        "Najdrahšie",
        "Najpredávanejšie",
        "Abecedne",
        "Všetky filtre",
        "Akcia dňa",
    ]
    return not any(x.lower() in t.lower() for x in bad_fragments)


def make_event(
    event_type: str,
    code: str,
    title: str,
    price: Optional[str],
    availability: Optional[str],
    url: Optional[str],
    payload: Dict[str, Any],
    changed_at: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "shop": SHOP,
        "event_type": event_type,
        "code": code,
        "title": title,
        "price": price,
        "availability": availability,
        "url": url,
        "changed_at": changed_at or utc_now(),
        "payload": payload,
    }


def ensure_db() -> None:
    with closing(db()) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS products_current (
                code TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                price TEXT,
                availability TEXT,
                url TEXT,
                payload_json TEXT,
                seen_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS products_known (
                code TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                price TEXT,
                availability TEXT,
                url TEXT,
                payload_json TEXT,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                shop TEXT NOT NULL,
                event_type TEXT NOT NULL,
                code TEXT,
                title TEXT,
                price TEXT,
                availability TEXT,
                url TEXT,
                changed_at TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS scan_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ok INTEGER NOT NULL,
                item_count INTEGER NOT NULL,
                elapsed_ms INTEGER NOT NULL,
                source_url TEXT NOT NULL,
                error_text TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_events_changed_at ON events(changed_at DESC);
            CREATE INDEX IF NOT EXISTS idx_events_code_changed_at ON events(code, changed_at DESC);
            CREATE INDEX IF NOT EXISTS idx_scan_runs_created_at ON scan_runs(created_at DESC);
            """
        )
        conn.commit()


def parse_products_from_dom(dom_items: List[Dict[str, Any]], source_url: str) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    seen_codes = set()

    for entry in dom_items:
        title = normalize_ws(entry.get("title"))
        url = normalize_ws(entry.get("url"))
        price = normalize_price(entry.get("price"))
        availability = normalize_availability(entry.get("availability"))
        image = normalize_ws(entry.get("image"))
        raw_text = normalize_ws(entry.get("raw_text"))

        if not looks_like_real_product(title, url, price, availability, raw_text):
            continue

        code = slug_from_url(url)
        if not code or code in seen_codes:
            continue

        seen_codes.add(code)
        items.append(
            {
                "shop": SHOP,
                "code": code,
                "title": title,
                "price": price,
                "availability": availability,
                "url": url,
                "is_available": is_available_now(availability),
                "payload": {
                    "source_url": source_url,
                    "raw_title": entry.get("title"),
                    "raw_price": entry.get("price"),
                    "raw_availability": entry.get("availability"),
                    "raw_text": raw_text,
                    "image": image or None,
                    "derived_code_from": "url_slug",
                },
            }
        )
    return items








def fetch_live_products() -> Dict[str, Any]:
    started = time.time()
    all_items: List[Dict[str, Any]] = []
    seen_codes = set()

    def accept_cookies(page) -> None:
        for selector in [
            "text=Prijať všetko",
            "button:has-text('Prijať všetko')",
            "text=Prijať",
            "button:has-text('Prijať')",
            "text=Accept",
            "button:has-text('Accept')",
        ]:
            try:
                page.locator(selector).first.click(timeout=1500)
                return
            except Exception:
                pass

    def safe_networkidle(page, timeout_ms: int = 10000) -> None:
        try:
            page.wait_for_load_state("networkidle", timeout=timeout_ms)
        except Exception:
            pass

    def collect_current_product_urls(page) -> List[str]:
        hrefs = page.evaluate(
            """
            () => {
              const out = [];
              const seen = new Set();
              const anchors = Array.from(document.querySelectorAll('a[href]'));
              for (const a of anchors) {
                let href = a.getAttribute('href') || a.href || '';
                if (!href) continue;
                try { href = new URL(href, location.origin).href; } catch (e) {}
                if (!href.startsWith('https://www.brloh.sk/')) continue;
                if (!/-p\d+\/?$/i.test(href)) continue;
                if (seen.has(href)) continue;
                seen.add(href);
                out.push(href);
              }
              return out;
            }
            """
        )
        return hrefs or []

    def expand_listing_via_load_more(page) -> List[str]:
        product_seen = set()
        stable_rounds = 0
        click_round = 0
        max_rounds = 40

        while click_round < max_rounds:
            click_round += 1

            # pozbieraj URL pred klikom
            urls_before = collect_current_product_urls(page)
            for u in urls_before:
                product_seen.add(u)

            # skús nájsť tlačidlo/link "Zobraziť ďalšie produkty"
            load_more = None
            candidate_selectors = [
                "text=Zobraziť ďalšie produkty",
                "a:has-text('Zobraziť ďalšie produkty')",
                "button:has-text('Zobraziť ďalšie produkty')",
                "text=Zobrazit dalsie produkty",
                "a:has-text('Zobrazit dalsie produkty')",
            ]

            for sel in candidate_selectors:
                try:
                    loc = page.locator(sel).first
                    if loc.count() > 0 and loc.is_visible():
                        load_more = loc
                        break
                except Exception:
                    pass

            if load_more is None:
                break

            try:
                load_more.scroll_into_view_if_needed(timeout=2000)
            except Exception:
                pass

            try:
                before_count = len(product_seen)
                load_more.click(timeout=5000)
            except Exception:
                break

            page.wait_for_timeout(1800)
            safe_networkidle(page, 8000)

            # po kliknutí jemne doscrolluj, nech sa stihnú dorenderovať ďalšie karty
            last_height = 0
            same_height = 0
            for _ in range(8):
                try:
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                except Exception:
                    pass
                page.wait_for_timeout(700)
                try:
                    h = page.evaluate("document.body.scrollHeight")
                except Exception:
                    h = last_height
                if h == last_height:
                    same_height += 1
                else:
                    same_height = 0
                    last_height = h
                if same_height >= 2:
                    break

            urls_after = collect_current_product_urls(page)
            for u in urls_after:
                product_seen.add(u)

            after_count = len(product_seen)
            if after_count == before_count:
                stable_rounds += 1
            else:
                stable_rounds = 0

            if stable_rounds >= 2:
                break

        return list(product_seen)

    def parse_detail_product(page, product_url: str) -> Optional[Dict[str, Any]]:
        try:
            page.goto(product_url, wait_until="domcontentloaded", timeout=45000)
            safe_networkidle(page, 8000)
            page.wait_for_timeout(600)

            data = page.evaluate(
                """
                () => {
                  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
                  const bodyText = norm(document.body ? (document.body.innerText || document.body.textContent || '') : '');

                  let title = '';
                  const h1 = document.querySelector('h1');
                  if (h1) title = norm(h1.textContent || '');

                  let image = '';
                  const og = document.querySelector('meta[property="og:image"]');
                  if (og) image = norm(og.getAttribute('content') || '');
                  if (!image) {
                    const img = document.querySelector('img[src], img[data-src], img[data-original]');
                    if (img) image = norm(img.currentSrc || img.getAttribute('src') || img.getAttribute('data-src') || img.getAttribute('data-original') || '');
                  }

                  let price = '';
                  const selectors = ['[class*="price"]', '[data-price]', '.price', '.product-price', '.price-box'];
                  for (const sel of selectors) {
                    const nodes = Array.from(document.querySelectorAll(sel));
                    for (const n of nodes) {
                      const txt = norm(n.textContent || '');
                      const m1 = txt.match(/\d[\d\s.,]*\s*€/i);
                      const m2 = txt.match(/€\s*\d[\d\s.,]*/i);
                      if (m1 || m2) {
                        price = norm((m1 && m1[0]) || (m2 && m2[0]) || txt);
                        break;
                      }
                    }
                    if (price) break;
                  }
                  if (!price) {
                    const m1 = bodyText.match(/\d[\d\s.,]*\s*€/i);
                    const m2 = bodyText.match(/€\s*\d[\d\s.,]*/i);
                    price = norm((m1 && m1[0]) || (m2 && m2[0]) || '');
                  }

                  let availability = '';
                  const patterns = [
                    /Na sklade\s*[><]?\s*\d+\s*ks/i,
                    /Na sklade/i,
                    /Posledných\s*\d+\s*(kusov|ks)/i,
                    /Predobjednávka/i,
                    /Na objednávku/i,
                    /Vypredané/i,
                    /Predaj ukončený/i,
                    /Nedostupné/i,
                    /Neznáma dostupnosť/i,
                    /Pripravujeme/i
                  ];
                  for (const p of patterns) {
                    const m = bodyText.match(p);
                    if (m) {
                      availability = norm(m[0]);
                      break;
                    }
                  }

                  return {
                    title,
                    price,
                    availability,
                    image,
                    raw_text: bodyText
                  };
                }
                """
            )

            title = normalize_ws(data.get("title"))
            price = normalize_price(data.get("price"))
            availability = normalize_availability(data.get("availability"))
            image = normalize_ws(data.get("image"))
            raw_text = normalize_ws(data.get("raw_text"))

            if not title:
                return None

            return {
                "shop": SHOP,
                "code": slug_from_url(product_url),
                "title": title,
                "price": price,
                "availability": availability,
                "url": product_url,
                "is_available": is_available_now(availability),
                "payload": {
                    "source_url": SOURCE_URL,
                    "raw_title": data.get("title"),
                    "raw_price": data.get("price"),
                    "raw_availability": data.get("availability"),
                    "raw_text": raw_text,
                    "image": image or None,
                    "derived_code_from": "url_slug",
                },
            }
        except Exception:
            return None

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        context = browser.new_context(
            user_agent=USER_AGENT,
            locale="sk-SK",
            timezone_id="Europe/Bratislava",
            viewport={"width": 1600, "height": 3200},
        )

        try:
            listing_page = context.new_page()
            try:
                source_url = SOURCE_URLS[0]
                listing_page.goto(source_url, wait_until="domcontentloaded", timeout=60000)
                accept_cookies(listing_page)
                safe_networkidle(listing_page, 12000)
                listing_page.wait_for_timeout(1500)

                product_urls = expand_listing_via_load_more(listing_page)

            finally:
                listing_page.close()

            detail_page = context.new_page()
            try:
                for product_url in product_urls:
                    item = parse_detail_product(detail_page, product_url)
                    if not item:
                        continue
                    code = item.get("code")
                    if not code or code in seen_codes:
                        continue
                    if not looks_like_real_product(
                        item.get("title", ""),
                        item.get("url", ""),
                        item.get("price"),
                        item.get("availability"),
                        item.get("payload", {}).get("raw_text", ""),
                    ):
                        continue
                    seen_codes.add(code)
                    all_items.append(item)
            finally:
                detail_page.close()

            elapsed_ms = int((time.time() - started) * 1000)
            return {
                "ok": True,
                "shop": SHOP,
                "source_urls": SOURCE_URLS,
                "elapsed_ms": elapsed_ms,
                "count": len(all_items),
                "items": all_items,
            }

        except PlaywrightTimeoutError as e:
            raise HTTPException(status_code=504, detail=f"Timeout while loading Brloh: {e}")
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Brloh parsing failed: {e}")
        finally:
            context.close()
            browser.close()

def insert_event(conn: sqlite3.Connection, event: Dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO events (shop, event_type, code, title, price, availability, url, changed_at, payload_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event["shop"],
            event["event_type"],
            event["code"],
            event["title"],
            event["price"],
            event["availability"],
            event["url"],
            event["changed_at"],
            json.dumps(event["payload"], ensure_ascii=False),
        ),
    )


def persist_scan(items: List[Dict[str, Any]], elapsed_ms: int, source_url: str = SOURCE_URL) -> Dict[str, Any]:
    now = utc_now()
    new_events: List[Dict[str, Any]] = []
    price_events: List[Dict[str, Any]] = []
    availability_events: List[Dict[str, Any]] = []
    disappeared_events: List[Dict[str, Any]] = []

    current_map = {item["code"]: item for item in items}

    with closing(db()) as conn:
        old_current_map = {row["code"]: dict(row) for row in conn.execute("SELECT * FROM products_current").fetchall()}
        known_map = {row["code"]: dict(row) for row in conn.execute("SELECT * FROM products_known").fetchall()}

        conn.execute("DELETE FROM products_current")

        for item in items:
            conn.execute(
                """
                INSERT OR REPLACE INTO products_current
                (code, title, price, availability, url, payload_json, seen_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item["code"],
                    item["title"],
                    item["price"],
                    item["availability"],
                    item["url"],
                    json.dumps(item["payload"], ensure_ascii=False),
                    now,
                ),
            )

            existing = known_map.get(item["code"])

            if existing is None:
                conn.execute(
                    """
                    INSERT INTO products_known
                    (code, title, price, availability, url, payload_json, first_seen_at, last_seen_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        item["code"],
                        item["title"],
                        item["price"],
                        item["availability"],
                        item["url"],
                        json.dumps(item["payload"], ensure_ascii=False),
                        now,
                        now,
                    ),
                )
                ev = make_event(
                    EVENT_TYPE_NEW_PRODUCT,
                    item["code"],
                    item["title"],
                    item["price"],
                    item["availability"],
                    item["url"],
                    {
                        "source_url": source_url,
                        "current": item,
                    },
                    now,
                )
                insert_event(conn, ev)
                new_events.append(ev)
                continue

            old_price = existing["price"]
            old_availability = existing["availability"]
            old_url = existing["url"]

            if normalize_price(old_price) != normalize_price(item["price"]):
                ev = make_event(
                    EVENT_TYPE_PRICE_CHANGE,
                    item["code"],
                    item["title"],
                    item["price"],
                    item["availability"],
                    item["url"] or old_url,
                    {
                        "source_url": source_url,
                        "old_price": old_price,
                        "new_price": item["price"],
                        "old_availability": old_availability,
                        "new_availability": item["availability"],
                    },
                    now,
                )
                insert_event(conn, ev)
                price_events.append(ev)

            if normalize_availability(old_availability) != normalize_availability(item["availability"]):
                ev = make_event(
                    EVENT_TYPE_AVAILABILITY_CHANGE,
                    item["code"],
                    item["title"],
                    item["price"],
                    item["availability"],
                    item["url"] or old_url,
                    {
                        "source_url": source_url,
                        "old_availability": old_availability,
                        "new_availability": item["availability"],
                        "old_price": old_price,
                        "new_price": item["price"],
                    },
                    now,
                )
                insert_event(conn, ev)
                availability_events.append(ev)

            conn.execute(
                """
                UPDATE products_known
                SET title = ?, price = ?, availability = ?, url = ?, payload_json = ?, last_seen_at = ?
                WHERE code = ?
                """,
                (
                    item["title"],
                    item["price"],
                    item["availability"],
                    item["url"],
                    json.dumps(item["payload"], ensure_ascii=False),
                    now,
                    item["code"],
                ),
            )

        # Brloh: product_disappeared events disabled.
        # Listing/filter source is not stable enough for reliable disappearance detection.
        # We intentionally do nothing here to avoid duplicate disappeared spam.

        conn.execute(
            """
            INSERT INTO scan_runs (ok, item_count, elapsed_ms, source_url, error_text, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (1, len(items), elapsed_ms, source_url, None, now),
        )

        conn.commit()

    return {
        "ok": True,
        "saved_count": len(items),
        "new_events": new_events,
        "price_events": price_events,
        "availability_events": availability_events,
        "disappeared_events": disappeared_events,
    }


def record_scan_error(error_text: str, source_url: str = SOURCE_URL) -> None:
    now = utc_now()
    with closing(db()) as conn:
        conn.execute(
            """
            INSERT INTO scan_runs (ok, item_count, elapsed_ms, source_url, error_text, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (0, 0, 0, source_url, error_text[:4000], now),
        )
        ev = make_event(
            EVENT_TYPE_SCAN_ERROR,
            "scan",
            "scan_error",
            None,
            None,
            source_url,
            {"source_url": source_url, "error": error_text[:4000]},
            now,
        )
        insert_event(conn, ev)
        conn.commit()



def push_to_aggregator(shop: str) -> Dict[str, Any]:
    if not AGGREGATOR_SYNC_ENABLED:
        return {"ok": False, "reason": "disabled"}

    url = f"{AGGREGATOR_BASE}{AGGREGATOR_INGEST_PATH}"
    params = {
        "source": shop,
        "trigger_scan": "0",
    }

    try:
        response = requests.post(url, params=params, timeout=180)
        response.raise_for_status()
        try:
            body = response.json()
        except Exception:
            body = {"ok": True, "status_code": response.status_code}
        return {
            "ok": True,
            "status_code": response.status_code,
            "body": body,
        }
    except Exception as e:
        return {
            "ok": False,
            "reason": str(e),
        }


def scan_once() -> Dict[str, Any]:
    ensure_db()
    live = fetch_live_products()
    persisted = persist_scan(live["items"], live["elapsed_ms"], SOURCE_URL)
    aggregator_sync_result = push_to_aggregator("brloh")
    return {
        "ok": True,
        "shop": SHOP,
        "source_urls": SOURCE_URLS,
        "elapsed_ms": live["elapsed_ms"],
        "count": live["count"],
        "new_count": len(persisted["new_events"]),
        "price_change_count": len(persisted["price_events"]),
        "availability_change_count": len(persisted["availability_events"]),
        "disappeared_count": len(persisted["disappeared_events"]),
        "new_events": persisted["new_events"],
        "price_events": persisted["price_events"],
        "availability_events": persisted["availability_events"],
        "disappeared_events": persisted["disappeared_events"],
        "aggregator_sync": aggregator_sync_result,
    }


def latest_scan_run() -> Optional[Dict[str, Any]]:
    with closing(db()) as conn:
        row = conn.execute(
            """
            SELECT id, ok, item_count, elapsed_ms, source_url, error_text, created_at
            FROM scan_runs
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        return dict(row) if row else None


def _row_to_item(row: sqlite3.Row) -> Dict[str, Any]:
    item = dict(row)
    try:
        item["payload"] = json.loads(item.pop("payload_json") or "{}")
    except Exception:
        item["payload"] = {}
    item["is_available"] = is_available_now(item.get("availability"))
    return item


def get_current_products(available_only: Optional[bool] = None) -> List[Dict[str, Any]]:
    with closing(db()) as conn:
        rows = conn.execute(
            """
            SELECT code, title, price, availability, url, payload_json, seen_at
            FROM products_current
            ORDER BY title COLLATE NOCASE ASC
            """
        ).fetchall()
    out = [_row_to_item(row) for row in rows]
    if available_only is True:
        out = [x for x in out if x["is_available"]]
    elif available_only is False:
        out = [x for x in out if not x["is_available"]]
    return out


def get_known_products() -> List[Dict[str, Any]]:
    with closing(db()) as conn:
        rows = conn.execute(
            """
            SELECT code, title, price, availability, url, payload_json, first_seen_at, last_seen_at
            FROM products_known
            ORDER BY last_seen_at DESC, title COLLATE NOCASE ASC
            """
        ).fetchall()
    return [_row_to_item(row) for row in rows]


def get_events(limit: int = 100) -> List[Dict[str, Any]]:
    with closing(db()) as conn:
        rows = conn.execute(
            """
            SELECT id, shop, event_type, code, title, price, availability, url, changed_at, payload_json
            FROM events
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        try:
            item["payload"] = json.loads(item.pop("payload_json") or "{}")
        except Exception:
            item["payload"] = {}
        out.append(item)
    return out


def get_product_history_by_code(code: str, limit: int = 100) -> List[Dict[str, Any]]:
    with closing(db()) as conn:
        rows = conn.execute(
            """
            SELECT id, shop, event_type, code, title, price, availability, url, changed_at, payload_json
            FROM events
            WHERE code = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (code, limit),
        ).fetchall()

    out = []
    for row in rows:
        item = dict(row)
        try:
            item["payload"] = json.loads(item.pop("payload_json") or "{}")
        except Exception:
            item["payload"] = {}
        out.append(item)
    return out


def summary() -> Dict[str, Any]:
    current_items = get_current_products()
    current_available = sum(1 for x in current_items if x["is_available"])
    current_unavailable = sum(1 for x in current_items if not x["is_available"])

    with closing(db()) as conn:
        known_count = conn.execute("SELECT COUNT(*) AS c FROM products_known").fetchone()["c"]
        total_events = conn.execute("SELECT COUNT(*) AS c FROM events").fetchone()["c"]
        row = conn.execute(
            """
            SELECT
                SUM(CASE WHEN event_type = ? THEN 1 ELSE 0 END) AS new_products,
                SUM(CASE WHEN event_type = ? THEN 1 ELSE 0 END) AS price_changes,
                SUM(CASE WHEN event_type = ? THEN 1 ELSE 0 END) AS availability_changes,
                SUM(CASE WHEN event_type = ? THEN 1 ELSE 0 END) AS disappeared
            FROM events
            """,
            (
                EVENT_TYPE_NEW_PRODUCT,
                EVENT_TYPE_PRICE_CHANGE,
                EVENT_TYPE_AVAILABILITY_CHANGE,
                EVENT_TYPE_PRODUCT_DISAPPEARED,
            ),
        ).fetchone()

    return {
        "ok": True,
        "shop": SHOP,
        "known_products": int(known_count or 0),
        "current_products": len(current_items),
        "current_products_available": int(current_available),
        "current_products_unavailable": int(current_unavailable),
        "total_events": int(total_events or 0),
        "new_products": int(row["new_products"] or 0),
        "price_changes": int(row["price_changes"] or 0),
        "availability_changes": int(row["availability_changes"] or 0),
        "disappeared_products": int(row["disappeared"] or 0),
    }


def markdown_to_html(md: str) -> str:
    lines = md.splitlines()
    out: List[str] = []
    list_depth = 0

    def close_lists(target: int = 0) -> None:
        nonlocal list_depth
        while list_depth > target:
            out.append("</ul>")
            list_depth -= 1

    for line in lines:
        raw = line.rstrip()
        stripped = raw.lstrip(" ")
        indent = len(raw) - len(stripped)

        if not stripped:
            close_lists(0)
            continue

        if stripped.startswith("# "):
            close_lists(0)
            out.append(f"<h1>{stripped[2:]}</h1>")
            continue

        if stripped.startswith("## "):
            close_lists(0)
            out.append(f"<h2>{stripped[3:]}</h2>")
            continue

        if stripped.startswith("### "):
            close_lists(0)
            out.append(f"<h3>{stripped[4:]}</h3>")
            continue

        if stripped.startswith("- "):
            target_depth = 1 + (indent // 2)
            while list_depth < target_depth:
                out.append("<ul>")
                list_depth += 1
            close_lists(target_depth)
            out.append(f"<li>{stripped[2:]}</li>")
            continue

        close_lists(0)
        out.append(f"<p>{stripped}</p>")

    close_lists(0)
    return "\n".join(out)


def read_changelog() -> str:
    try:
        return CHANGELOG_PATH.read_text(encoding="utf-8")
    except Exception:
        return "# Changelog\n\n## [0.1.0] - 2026-03-31\n\n- Changelog file missing."


def frontend_html() -> str:
    html = r"""<!doctype html>
<html lang="sk">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>brloh-parser</title>
  <style>
    :root{
      --bg:#081225;
      --bg2:#0b1730;
      --panel:#0b1830;
      --panel2:#0d1b34;
      --line:#263754;
      --text:#e7eefb;
      --muted:#99abc8;
      --green:#22c55e;
      --red:#ef4444;
      --blue:#2563eb;
      --blue2:#1d4ed8;
      --amber:#f59e0b;
      --shadow:0 10px 28px rgba(0,0,0,.28);
      --radius:18px;
      --panel-h:836px;
    }

    *{box-sizing:border-box}
    html,body{margin:0;padding:0;height:100%}
    body{
      font-family:Inter,Arial,Helvetica,sans-serif;
      background:linear-gradient(180deg,var(--bg) 0%, var(--bg2) 100%);
      color:var(--text);
      padding:24px;
    }

    a{color:#6ea8ff;text-decoration:none}
    a:hover{text-decoration:underline}

    .wrap{max-width:1880px;margin:0 auto}

    .topbar{
      display:flex;
      justify-content:space-between;
      align-items:flex-start;
      gap:16px;
      margin-bottom:18px;
      flex-wrap:wrap;
    }

    .brand h1{
      margin:0;
      font-size:32px;
      line-height:1.05;
      font-weight:800;
      letter-spacing:-.03em;
    }

    .version{
      font-size:16px;
      color:var(--muted);
      font-weight:700;
      margin-left:8px;
    }

    .subtitle{
      margin-top:4px;
      color:var(--muted);
      font-size:15px;
    }

    .toolbar{
      display:flex;
      gap:10px;
      flex-wrap:wrap;
      align-items:center;
    }

    .btn{
      background:var(--blue);
      color:#fff;
      border:0;
      border-radius:12px;
      padding:10px 14px;
      cursor:pointer;
      font-weight:700;
      box-shadow:var(--shadow);
    }
    .btn:hover{background:var(--blue2)}

    .dashboard{
      display:grid;
      grid-template-columns: 1fr 1fr 1fr 1fr 1.15fr;
      gap:18px;
      align-items:stretch;
    }

    .col{
      min-width:0;
      display:flex;
      flex-direction:column;
      gap:18px;
    }

    .card{
      background:linear-gradient(180deg, rgba(12,25,50,.98) 0%, rgba(10,21,40,.98) 100%);
      border:1px solid var(--line);
      border-radius:var(--radius);
      box-shadow:var(--shadow);
      overflow:hidden;
      min-width:0;
      height:var(--panel-h);
      display:flex;
      flex-direction:column;
    }

    .card.compact{
      height:calc((var(--panel-h) - 9px) / 2);
    }

    .card-head{
      padding:18px 18px 10px;
      font-size:24px;
      font-weight:800;
      letter-spacing:-.02em;
      flex:0 0 auto;
    }

    .card-body{
      padding:0 18px 18px;
      min-height:0;
      flex:1 1 auto;
      overflow:auto;
    }

    .stats-list,.feed-list{
      display:flex;
      flex-direction:column;
    }

    .stats-row,.feed-item,.stock-item{
      border-top:1px solid var(--line);
    }

    .stats-row{
      display:flex;
      justify-content:space-between;
      gap:10px;
      padding:12px 0;
    }

    .stats-row .label{color:var(--text)}
    .stats-row .value{
      color:var(--text);
      text-align:right;
      font-weight:700;
      word-break:break-word;
    }

    .ok{color:var(--green);font-weight:800}
    .bad{color:var(--red);font-weight:800}
    .blue{color:#7cb2ff;font-weight:800}
    .amber{color:var(--amber);font-weight:800}
    .muted{color:var(--muted)}
    .mono{
      font-family:Menlo,Monaco,Consolas,monospace;
      letter-spacing:.01em;
      word-break:break-word;
    }

    .feed-item,.stock-item{padding:14px 0}

    .product-title{
      font-size:17px;
      font-weight:800;
      line-height:1.22;
      margin-bottom:6px;
      word-break:break-word;
    }

    .product-price{
      font-size:14px;
      font-weight:700;
      color:var(--text);
    }

    .availability{
      font-size:14px;
      font-weight:800;
    }

    .availability.in-stock{color:var(--green)}
    .availability.out-stock{color:var(--red)}

    .meta{
      display:flex;
      gap:8px;
      flex-wrap:wrap;
      align-items:center;
      color:var(--muted);
      font-size:13px;
      margin-top:4px;
    }

    .code{
      margin-top:4px;
      font-size:12px;
      color:#b9c7de;
      word-break:break-word;
    }

    .event-type{
      font-weight:900;
      font-size:14px;
      text-transform:none;
    }
    .event-type.new_product{color:var(--green)}
    .event-type.price_change{color:var(--amber)}
    .event-type.availability_change{color:#7cb2ff}
    .event-type.product_disappeared{color:var(--red)}
    .event-type.scan_error{color:var(--red)}

    .empty{
      color:var(--muted);
      padding:8px 0 4px;
    }

    .stock-section{
      display:flex;
      flex-direction:column;
      gap:18px;
      min-height:100%;
    }

    .stock-group-title{
      position:sticky;
      top:0;
      z-index:2;
      padding:10px 0 12px;
      font-size:14px;
      font-weight:900;
      letter-spacing:.04em;
      background:linear-gradient(180deg, rgba(10,21,40,1) 0%, rgba(10,21,40,.98) 100%);
      border-bottom:1px solid var(--line);
    }

    .stock-group-title.in-stock{color:var(--green)}
    .stock-group-title.out-stock{color:var(--red)}

    .history-btn{
      margin-top:8px;
      background:#1e40af;
      color:#fff;
      border:0;
      border-radius:8px;
      padding:8px 10px;
      cursor:pointer;
      font-weight:700;
    }
    .history-btn:hover{background:#1d4ed8}

    .modal{
      display:none;
      position:fixed;
      inset:0;
      background:rgba(2,6,23,.72);
      z-index:9999;
      padding:24px;
    }
    .modal.show{display:block}

    .modal-card{
      max-width:920px;
      margin:0 auto;
      background:#111827;
      border:1px solid #334155;
      border-radius:14px;
      padding:16px;
      max-height:85vh;
      overflow:auto;
    }

    .modal-head{
      display:flex;
      justify-content:space-between;
      align-items:center;
      gap:12px;
      margin-bottom:8px;
    }

    .close-btn{
      background:#334155;
      color:#fff;
      border:0;
      border-radius:8px;
      padding:8px 10px;
      cursor:pointer;
      font-weight:700;
    }

    @media (max-width:1700px){
      .dashboard{
        grid-template-columns:1fr 1fr 1fr 1fr 1.1fr;
      }
    }

    @media (max-width:1450px){
      .dashboard{
        grid-template-columns:1fr 1fr;
      }
      .card,.card.compact{height:620px}
    }

    @media (max-width:900px){
      body{padding:16px}
      .dashboard{grid-template-columns:1fr}
      .brand h1{font-size:28px}
      .card-head{font-size:22px}
      .card,.card.compact{height:auto; min-height:420px}
    }
  </style>
</head>
<body>
<div class="wrap">
  <div class="topbar">
    <div class="brand">
      <h1>brloh-parser <a href="/changelog"><span class="version">v__APP_VERSION__</span></a></h1>
      <div class="subtitle">Sesterský parser k pikazard-parseru pre BRLOH.sk</div>
    </div>
    <div class="toolbar">
      <button class="btn" onclick="runScan()">Spustiť scan</button>
    </div>
  </div>

  <div class="dashboard">
    <div class="col">
      <section class="card compact">
        <div class="card-head">Stav</div>
        <div class="card-body">
          <div class="stats-list" id="health-box">Načítavam…</div>
        </div>
      </section>

      <section class="card compact">
        <div class="card-head">Súhrn</div>
        <div class="card-body">
          <div class="stats-list" id="summary-box">Načítavam…</div>
        </div>
      </section>
    </div>

    <div class="col">
      <section class="card">
        <div class="card-head">Posledné udalosti</div>
        <div class="card-body">
          <div class="feed-list" id="events-box">Načítavam…</div>
        </div>
      </section>
    </div>

    <div class="col">
      <section class="card">
        <div class="card-head">Posledné zmeny cien</div>
        <div class="card-body">
          <div class="feed-list" id="price-events-box">Načítavam…</div>
        </div>
      </section>
    </div>

    <div class="col">
      <section class="card">
        <div class="card-head">Posledné zmeny dostupnosti</div>
        <div class="card-body">
          <div class="feed-list" id="availability-events-box">Načítavam…</div>
        </div>
      </section>
    </div>

    <div class="col">
      <section class="card">
        <div class="card-head">Aktuálne produkty</div>
        <div class="card-body">
          <div class="stock-section">
            <div>
              <div class="stock-group-title in-stock">NA SKLADE <span id="in-stock-count">0</span></div>
              <div class="feed-list" id="current-box">Načítavam…</div>
            </div>

            <div>
              <div class="stock-group-title out-stock">MIMO SKLADU <span id="out-stock-count">0</span></div>
              <div class="feed-list" id="unavailable-box">Načítavam…</div>
            </div>
          </div>
        </div>
      </section>
    </div>
  </div>
</div>

<div id="history-modal" class="modal" onclick="closeHistoryModal(event)">
  <div class="modal-card" onclick="event.stopPropagation()">
    <div class="modal-head">
      <h2 id="history-modal-title" style="margin:0;">História</h2>
      <button class="close-btn" onclick="closeHistoryModal()">Zavrieť</button>
    </div>
    <div id="history-modal-body" class="muted">Načítavam…</div>
  </div>
</div>

<script>
async function j(url, opts) {
  const r = await fetch(url, opts || {});
  if (!r.ok) throw new Error(await r.text());
  return await r.json();
}

function esc(v) {
  return String(v ?? "").replace(/[&<>"]/g, s => ({ "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;" }[s]));
}

function fmtDate(v) {
  if (!v) return "—";
  try {
    return new Intl.DateTimeFormat("sk-SK", {
      timeZone: "Europe/Bratislava",
      day: "2-digit",
      month: "2-digit",
      year: "numeric",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false
    }).format(new Date(v));
  } catch (e) {
    return String(v);
  }
}

function eventClass(t) {
  if (t === "new_product") return "new_product";
  if (t === "price_change") return "price_change";
  if (t === "availability_change") return "availability_change";
  if (t === "product_disappeared") return "product_disappeared";
  if (t === "scan_error") return "scan_error";
  return "";
}

function formatPriceDelta(current, previous) {
  const c = Number(String(current ?? "").replace(/[^\d,.-]/g, "").replace(",", "."));
  const p = Number(String(previous ?? "").replace(/[^\d,.-]/g, "").replace(",", "."));
  if (!Number.isFinite(c) || !Number.isFinite(p)) return "";
  const delta = +(c - p).toFixed(2);
  if (delta === 0) return "";
  return delta > 0 ? `+${delta.toFixed(2)} €` : `${delta.toFixed(2)} €`;
}

async function openHistoryModal(code, title) {
  const modal = document.getElementById("history-modal");
  const body = document.getElementById("history-modal-body");
  const heading = document.getElementById("history-modal-title");
  if (!modal || !body || !heading) return;

  heading.textContent = `História: ${title || code || ""}`;
  body.innerHTML = `<div class="muted">Načítavam...</div>`;
  modal.classList.add("show");

  try {
    const res = await j(`/api/products/history?code=${encodeURIComponent(code)}&limit=100`);
    if (!res.items || !res.items.length) {
      body.innerHTML = `<div class="muted">Pre tento produkt zatiaľ nie je história.</div>`;
      return;
    }

    body.innerHTML = res.items.map(item => {
      const payload = item.payload || {};
      const oldPrice = payload.old_price || "";
      const newPrice = payload.new_price || item.price || "";
      const oldAvail = payload.old_availability || "";
      const newAvail = payload.new_availability || item.availability || "";
      const delta = formatPriceDelta(newPrice, oldPrice);

      let extra = "";
      if (item.event_type === "price_change") {
        extra = `<div class="muted">Cena: <b>${esc(oldPrice || "—")}</b> → <b>${esc(newPrice || "—")}</b>${delta ? ` <span class="${delta.startsWith("+") ? "bad" : "ok"}">(${esc(delta)})</span>` : ""}</div>`;
      } else if (item.event_type === "availability_change") {
        extra = `<div class="muted">Dostupnosť: <b>${esc(oldAvail || "—")}</b> → <b>${esc(newAvail || "—")}</b></div>`;
      } else if (item.event_type === "new_product") {
        extra = `<div class="muted">Prvé zachytenie produktu.</div>`;
      } else if (item.event_type === "product_disappeared") {
        extra = `<div class="muted">Produkt zmizol z aktuálneho výpisu.</div>`;
      }

      return `
        <div class="feed-item">
          <div class="event-type ${eventClass(item.event_type)}">${esc(item.event_type)}</div>
          <div class="product-title">${esc(item.title || "")}</div>
          <div class="meta"><span>${esc(item.price || "—")}</span><span>|</span><span>${esc(item.availability || "—")}</span></div>
          ${extra}
          <div class="meta"><span>${fmtDate(item.changed_at)}</span></div>
        </div>
      `;
    }).join("");
  } catch (e) {
    body.innerHTML = `<div class="bad">Chyba pri načítaní histórie: ${esc(e.message)}</div>`;
  }
}

function closeHistoryModal(event) {
  if (event && event.target && event.target.id !== "history-modal") return;
  const modal = document.getElementById("history-modal");
  if (modal) modal.classList.remove("show");
}

function renderStatsRows(target, rows) {
  target.innerHTML = rows.map(r => `
    <div class="stats-row">
      <div class="label">${esc(r.label)}</div>
      <div class="value ${r.cls || ""}">${r.html !== undefined ? r.html : esc(r.value ?? "—")}</div>
    </div>
  `).join("");
}

function renderStockList(target, items, emptyText, inStock) {
  if (!items || !items.length) {
    target.innerHTML = `<div class="empty">${esc(emptyText)}</div>`;
    return;
  }

  target.innerHTML = items.map(i => `
    <div class="stock-item">
      <div class="product-title">${inStock ? "✔ " : "✖ "}${esc(i.title || "")}</div>
      <div class="meta">
        <span>${esc(i.price || "—")}</span>
        <span>|</span>
        <span class="availability ${inStock ? "in-stock" : "out-stock"}">${esc(i.availability || "—")}</span>
      </div>
      <div class="code mono">${esc(i.code || "")}</div>
      <div class="meta">
        ${i.url ? `<a href="${esc(i.url)}" target="_blank" rel="noopener noreferrer">otvoriť</a>` : ""}
      </div>
      <div><button class="history-btn" type="button" onclick="openHistoryModal('${esc(i.code)}','${esc((i.title || '').replace(/'/g, '&#39;'))}')">História</button></div>
    </div>
  `).join("");
}

function renderEventList(target, items, emptyText) {
  if (!items || !items.length) {
    target.innerHTML = `<div class="empty">${esc(emptyText)}</div>`;
    return;
  }
  target.innerHTML = items.map(e => `
    <div class="feed-item">
      <div class="event-type ${eventClass(e.event_type)}">${esc(e.event_type || "")}</div>
      <div class="product-title">${esc(e.title || "")}</div>
      <div class="meta">
        <span>${esc(e.price || "—")}</span>
        <span>|</span>
        <span>${esc(e.availability || "—")}</span>
      </div>
      <div class="code mono">${esc(e.code || "")}</div>
      <div class="meta"><span>${fmtDate(e.changed_at)}</span></div>
    </div>
  `).join("");
}

async function loadAll() {
  const [health, summary, current, unavailable, events] = await Promise.all([
    j("/health"),
    j("/api/summary"),
    j("/api/current?available_only=true"),
    j("/api/current?available_only=false"),
    j("/api/events?limit=60")
  ]);

  const allEvents = events.items || [];
  const priceEvents = allEvents.filter(x => x.event_type === "price_change");
  const availabilityEvents = allEvents.filter(x => x.event_type === "availability_change");

  const currentItems = current.items || [];
  const unavailableItems = unavailable.items || [];

  document.getElementById("in-stock-count").textContent = String(currentItems.length);
  document.getElementById("out-stock-count").textContent = String(unavailableItems.length);

  renderStatsRows(document.getElementById("health-box"), [
    { label: "Service", value: health.service || "brloh-parser" },
    { label: "Verzia", value: health.app_version || "—" },
    { label: "Port", value: health.port || "—" },
    { label: "DB", value: health.db_path || "—" },
    { label: "Posledný scan", value: health.latest_scan?.created_at ? fmtDate(health.latest_scan.created_at) : "—" },
    { label: "Posledný výsledok", html: health.latest_scan ? `<span class="${health.latest_scan.ok ? "ok" : "bad"}">${health.latest_scan.ok ? "OK" : "ERROR"}</span>` : "—" },
  ]);

  renderStatsRows(document.getElementById("summary-box"), [
    { label: "Known products", value: summary.known_products ?? 0 },
    { label: "Current products", value: summary.current_products ?? 0 },
    { label: "Skladom", html: `<span class="ok">${summary.current_products_available ?? currentItems.length}</span>` },
    { label: "Mimo skladu", html: `<span class="bad">${summary.current_products_unavailable ?? unavailableItems.length}</span>` },
    { label: "Total events", value: summary.total_events ?? 0 },
    { label: "New products", html: `<span class="ok">${summary.new_products ?? 0}</span>` },
    { label: "Price changes", html: `<span class="amber">${summary.price_changes ?? 0}</span>` },
    { label: "Availability changes", html: `<span class="blue">${summary.availability_changes ?? 0}</span>` },
    { label: "Disappeared", html: `<span class="bad">${summary.disappeared_products ?? 0}</span>` },
  ]);

  renderStockList(document.getElementById("current-box"), currentItems, "Žiadne produkty na sklade.", true);
  renderStockList(document.getElementById("unavailable-box"), unavailableItems, "Žiadne vypredané produkty.", false);
  renderEventList(document.getElementById("events-box"), allEvents, "Žiadne eventy.");
  renderEventList(document.getElementById("price-events-box"), priceEvents, "Zatiaľ bez zmien cien.");
  renderEventList(document.getElementById("availability-events-box"), availabilityEvents, "Zatiaľ bez zmien dostupnosti.");
}

async function runScan() {
  try {
    const r = await j("/api/scan", { method: "POST" });
    await loadAll();
    alert(
      "Scan hotový\n\n" +
      "Nové produkty: " + r.new_count + "\n" +
      "Zmeny cien: " + r.price_change_count + "\n" +
      "Zmeny dostupnosti: " + r.availability_change_count + "\n" +
      "Zmiznuté produkty: " + r.disappeared_count
    );
  } catch (e) {
    alert("Chyba pri scane: " + e.message);
  }
}

loadAll().catch(e => alert("Chyba pri načítaní: " + e.message));

(function () {
  if (window.__brlohAutoReloadInstalled) return;
  window.__brlohAutoReloadInstalled = true;

  let lastScanKey = null;
  let firstPassDone = false;
  let reloadInProgress = false;

  function scanKeyFromHealth(data) {
    const ls = data && data.latest_scan ? data.latest_scan : null;
    if (!ls) return null;
    const id = ls.id ?? "";
    const created = ls.created_at ?? "";
    const ok = ls.ok ?? "";
    const count = ls.item_count ?? "";
    return [id, created, ok, count].join("|");
  }

  async function pollLatestScan() {
    if (reloadInProgress) return;

    try {
      const res = await fetch("/health?_ts=" + Date.now(), { cache: "no-store" });
      if (!res.ok) return;

      const data = await res.json();
      const currentKey = scanKeyFromHealth(data);

      if (!firstPassDone) {
        lastScanKey = currentKey;
        firstPassDone = true;
        return;
      }

      if (currentKey && lastScanKey && currentKey !== lastScanKey) {
        reloadInProgress = true;
        window.location.reload();
        return;
      }

      if (currentKey && !lastScanKey) {
        reloadInProgress = true;
        window.location.reload();
        return;
      }

      lastScanKey = currentKey;
    } catch (e) {
    }
  }

  window.addEventListener("load", function () {
    setTimeout(pollLatestScan, 4000);
    setInterval(pollLatestScan, 15000);
  });
})();
</script>
</body>
</html>"""
    return html.replace("__APP_VERSION__", APP_VERSION)


@app.on_event("startup")
def startup() -> None:
    ensure_db()


@app.get("/health")
def health():
    return {
        "ok": True,
        "service": "brloh-parser",
        "app_version": APP_VERSION,
        "port": APP_PORT,
        "db_path": str(DB_PATH),
        "latest_scan": latest_scan_run(),
    }


@app.get("/api/summary")
def api_summary():
    return JSONResponse(summary())


@app.get("/api/current")
def api_current(available_only: Optional[bool] = Query(None)):
    items = get_current_products(available_only=available_only)
    return JSONResponse({"ok": True, "count": len(items), "items": items})


@app.get("/api/known")
def api_known():
    items = get_known_products()
    return JSONResponse({"ok": True, "count": len(items), "items": items})


@app.get("/api/events")
def api_events(limit: int = Query(100, ge=1, le=500)):
    items = get_events(limit=limit)
    return JSONResponse({"ok": True, "count": len(items), "items": items})


@app.get("/api/products/history")
def api_products_history(code: str = Query(...), limit: int = Query(100, ge=1, le=500)):
    items = get_product_history_by_code(code=code, limit=limit)
    return JSONResponse({"ok": True, "count": len(items), "items": items})


@app.get("/api/search/live")
def api_search_live():
    return JSONResponse(fetch_live_products())


@app.post("/api/scan")
def api_scan():
    try:
        return JSONResponse(scan_once())
    except Exception as e:
        error_text = str(e)
        record_scan_error(error_text, SOURCE_URL)
        raise HTTPException(status_code=500, detail=error_text)


@app.get("/changelog", response_class=HTMLResponse)
def changelog():
    body = markdown_to_html(read_changelog())
    return HTMLResponse(
        "<!doctype html><html lang='sk'><head>"
        "<meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>Changelog</title>"
        "<style>"
        "body{font-family:Arial,Helvetica,sans-serif;background:#0f172a;color:#e2e8f0;margin:0;padding:24px}"
        "a{color:#60a5fa;text-decoration:none}"
        "li{margin-bottom:8px}"
        "h1,h2,h3{margin-top:0}"
        "</style></head><body>"
        "<p><a href='/'>← späť</a></p>"
        f"{body}"
        "</body></html>"
    )


@app.get("/", response_class=HTMLResponse)
def frontend():
    return HTMLResponse(frontend_html())


if __name__ == "__main__":
    ensure_db()
    if len(sys.argv) > 1 and sys.argv[1] == "scan-once":
        try:
            result = scan_once()
            print(json.dumps(result, ensure_ascii=False, indent=2))
            raise SystemExit(0)
        except Exception as e:
            error_text = str(e)
            record_scan_error(error_text, SOURCE_URL)
            print(json.dumps({"ok": False, "error": error_text}, ensure_ascii=False, indent=2))
            raise SystemExit(1)

    import uvicorn
    uvicorn.run(app, host=APP_HOST, port=APP_PORT)
