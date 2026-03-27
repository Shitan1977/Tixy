from __future__ import annotations

import os
import random
import re
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import List, Optional

from bs4 import BeautifulSoup
from django.utils import timezone
from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.firefox.service import Service

from api.models import EventoPiattaforma, Performance, Piattaforma


SEED_FILE = "fansale_seed_prices.txt"


# =========================================================
# Helpers
# =========================================================

def normalize_text(value: Optional[str]) -> str:
    if not value:
        return ""
    value = value.replace("\xa0", " ")
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def normalize_artist_url(url: str) -> str:
    return url.split("#")[0].strip()


def is_valid_artist_url(url: str) -> bool:
    return re.fullmatch(r"https://www\.fansale\.it/tickets/all/[^/]+/\d+", url) is not None


def load_seed_artist_urls(filepath: str = SEED_FILE) -> List[str]:
    urls: List[str] = []

    try:
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                url = normalize_artist_url(line.strip())
                if not url:
                    continue
                if not is_valid_artist_url(url):
                    continue
                urls.append(url)
    except FileNotFoundError:
        pass

    seen = set()
    ordered = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            ordered.append(u)

    return ordered


def detect_access_denied(html: str, title: str) -> bool:
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True).lower()
    title_l = normalize_text(title).lower()

    signals = [
        "access denied",
        "you don't have permission",
        "forbidden",
        "blocked",
        "errors.edgesuite.net",
    ]
    return any(s in title_l for s in signals) or any(s in text for s in signals)


def save_debug_html(artist_url: str, html: str) -> str:
    debug_dir = Path("debug_fansale_prices")
    debug_dir.mkdir(exist_ok=True)

    parts = artist_url.rstrip("/").split("/")
    slug = parts[-2] if len(parts) >= 2 else "unknown"
    artist_id = parts[-1] if len(parts) >= 1 else "unknown"

    filename = debug_dir / f"{slug}_{artist_id}.html"
    filename.write_text(html, encoding="utf-8")
    return str(filename)


def sleep_human(min_seconds: float = 2.5, max_seconds: float = 5.5):
    time.sleep(random.uniform(min_seconds, max_seconds))


# =========================================================
# Selenium driver
# =========================================================

def get_firefox_binary() -> str:
    paths = [
        "/snap/firefox/current/usr/lib/firefox/firefox",
        "/usr/bin/firefox",
    ]
    for p in paths:
        if os.path.exists(p):
            return p
    raise RuntimeError("Firefox non trovato")


def build_driver():
    options = Options()
    options.add_argument("--headless")
    options.binary_location = get_firefox_binary()

    options.set_preference(
        "general.useragent.override",
        "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"
    )
    options.set_preference("dom.webdriver.enabled", False)
    options.set_preference("useAutomationExtension", False)
    options.set_preference("media.peerconnection.enabled", False)

    service = Service()

    driver = webdriver.Firefox(service=service, options=options)
    driver.set_window_size(1600, 1200)

    try:
        driver.execute_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
    except Exception:
        pass

    return driver


def fetch_html_with_driver(driver, url: str, wait_seconds: int = 8) -> str:
    driver.get(url)
    time.sleep(4)

    try:
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(2)
        driver.execute_script("window.scrollTo(0, 0);")
        time.sleep(1)
    except Exception:
        pass

    end_time = time.time() + wait_seconds
    last_html = ""

    while time.time() < end_time:
        html = driver.page_source
        last_html = html

        if "/tickets/all/" in html:
            return html

        time.sleep(1)

    return last_html


# =========================================================
# Price helpers
# =========================================================

def parse_price_text_to_decimal(price_text: str) -> Optional[Decimal]:
    if not price_text:
        return None

    txt = normalize_text(price_text).lower()
    m = re.search(r"(\d{1,3}(?:\.\d{3})*,\d{2}|\d+,\d{2})", txt)
    if not m:
        return None

    raw = m.group(1).replace(".", "").replace(",", ".")
    try:
        value = Decimal(raw)
    except (InvalidOperation, ValueError):
        return None

    if value <= 0:
        return None

    return value


def extract_price_from_context(text: str) -> tuple[Optional[str], Optional[Decimal], Optional[str]]:
    if not text:
        return None, None, None

    text = normalize_text(text)

    patterns = [
        r"offerte?\s+da\s*(?:€|eur)?\s*\d{1,3}(?:\.\d{3})*,\d{2}",
        r"prezzo\s+fisso\s*(?:€|eur)?\s*\d{1,3}(?:\.\d{3})*,\d{2}",
        r"\bda\s*(?:€|eur)?\s*\d{1,3}(?:\.\d{3})*,\d{2}",
        r"(?:€|eur)\s*\d{1,3}(?:\.\d{3})*,\d{2}",
    ]

    for pattern in patterns:
        m = re.search(pattern, text, flags=re.IGNORECASE)
        if m:
            price_text = normalize_text(m.group(0))
            price_value = parse_price_text_to_decimal(price_text)
            if price_value is not None:
                return price_text, price_value, "EUR"

    return None, None, None


def get_context_text(a_tag, max_levels: int = 4) -> str:
    texts = []

    try:
        txt = a_tag.get_text(" ", strip=True)
        txt = normalize_text(txt)
        if txt:
            texts.append(txt)
    except Exception:
        pass

    current = a_tag
    for _ in range(max_levels):
        try:
            current = current.parent
            if current is None:
                break
            txt = normalize_text(current.get_text(" ", strip=True))
            if txt:
                texts.append(txt)
        except Exception:
            break

    texts = [t for t in texts if t]
    if not texts:
        return ""

    texts.sort(key=len, reverse=True)
    return texts[0]


# =========================================================
# Price dataclass
# =========================================================

@dataclass
class FanSalePriceData:
    offer_id: str
    offer_url: str
    artist_url: str
    price_text: Optional[str]
    price_value: Optional[Decimal]
    currency: Optional[str]
    context_text: str


# =========================================================
# Parsing artist page
# =========================================================

def parse_artist_page_prices(
    *,
    html: str,
    artist_url: str,
    seen_ids: set,
    verbose: bool = False,
) -> List[FanSalePriceData]:
    items: List[FanSalePriceData] = []
    soup = BeautifulSoup(html, "html.parser")
    links = soup.find_all("a", href=True)

    if verbose:
        print("[LINK COUNT]", len(links))

    for a in links:
        href = normalize_text(a.get("href", ""))

        if not re.match(r"^/tickets/all/[^/]+/\d+/\d+$", href):
            continue

        offer_id = href.rstrip("/").split("/")[-1]

        if offer_id in seen_ids:
            continue

        context_text = get_context_text(a, max_levels=4)
        price_text, price_value, currency = extract_price_from_context(context_text)

        item = FanSalePriceData(
            offer_id=offer_id,
            offer_url="https://www.fansale.it" + href,
            artist_url=artist_url,
            price_text=price_text,
            price_value=price_value,
            currency=currency,
            context_text=context_text,
        )

        seen_ids.add(offer_id)
        items.append(item)

        if verbose:
            print("[PRICE]", item.offer_id, "|", item.price_text, "|", item.price_value)

    return items


# =========================================================
# DB update
# =========================================================

def get_target_performance(evento) -> Optional[Performance]:
    return (
        Performance.objects
        .filter(evento=evento)
        .order_by("starts_at_utc")
        .first()
    )


def update_single_price(item: FanSalePriceData, verbose: bool = False) -> str:
    piattaforma, _ = Piattaforma.objects.get_or_create(
        nome="fansale",
        defaults={
            "dominio": "fansale.it",
            "attivo": True,
        },
    )

    ep = (
        EventoPiattaforma.objects
        .select_related("evento")
        .filter(
            piattaforma=piattaforma,
            id_evento_piattaforma=item.offer_id,
        )
        .first()
    )

    if not ep:
        return "not_in_db"

    perf = get_target_performance(ep.evento)
    if not perf:
        ep.ultima_scansione = timezone.now()
        ep.snapshot_raw = {
            "fansale_price_found": False,
            "fansale_price_text": None,
            "fansale_price_value": None,
            "fansale_currency": None,
            "source_url": item.offer_url,
            "artist_url": item.artist_url,
            "offer_id": item.offer_id,
            "reason": "no_performance",
            "context_text": item.context_text[:1000],
        }
        ep.save(update_fields=["ultima_scansione", "snapshot_raw", "aggiornato_il"])
        return "no_performance"

    if item.price_value is None:
        ep.ultima_scansione = timezone.now()
        ep.snapshot_raw = {
            "fansale_price_found": False,
            "fansale_price_text": None,
            "fansale_price_value": None,
            "fansale_price_currency": None,
            "source_url": item.offer_url,
            "artist_url": item.artist_url,
            "offer_id": item.offer_id,
            "reason": "price_not_found_from_artist_context",
            "context_text": item.context_text[:1000],
        }
        ep.save(update_fields=["ultima_scansione", "snapshot_raw", "aggiornato_il"])
        return "price_not_found"

    changed_perf = False

    if perf.prezzo_min != item.price_value:
        perf.prezzo_min = item.price_value
        changed_perf = True

    if perf.prezzo_max != item.price_value:
        perf.prezzo_max = item.price_value
        changed_perf = True

    if perf.valuta != (item.currency or "EUR"):
        perf.valuta = item.currency or "EUR"
        changed_perf = True

    if changed_perf:
        perf.save(update_fields=["prezzo_min", "prezzo_max", "valuta", "aggiornato_il"])

    ep.ultima_scansione = timezone.now()
    ep.snapshot_raw = {
        "fansale_price_found": True,
        "fansale_price_text": item.price_text,
        "fansale_price_value": str(item.price_value),
        "fansale_price_currency": item.currency or "EUR",
        "source_url": item.offer_url,
        "artist_url": item.artist_url,
        "offer_id": item.offer_id,
        "reason": "price_found_from_artist_context",
        "context_text": item.context_text[:1000],
    }
    ep.save(update_fields=["ultima_scansione", "snapshot_raw", "aggiornato_il"])

    if verbose:
        print(f"[UPDATED] offer_id={item.offer_id} price={item.price_value}")

    return "updated"


# =========================================================
# Scraper
# =========================================================

def fetch_fansale_prices(limit_artists: int = 0, verbose: bool = False):
    items: List[FanSalePriceData] = []
    artist_reports: List[dict] = []
    seen_ids = set()

    artists = load_seed_artist_urls()
    if limit_artists > 0:
        artists = artists[:limit_artists]

    print("[SEED PRICE ARTIST URLS TOTAL]", len(artists))

    if not artists:
        return items, artist_reports

    for idx, artist_url in enumerate(artists, start=1):
        report = {
            "artist_url": artist_url,
            "found_in_artist": 0,
            "blocked": False,
            "empty": False,
        }

        if verbose:
            print(f"[ARTIST {idx}/{len(artists)}] {artist_url}")

        driver = None
        try:
            driver = build_driver()
            sleep_human(1.5, 3.5)

            html = fetch_html_with_driver(driver, artist_url, wait_seconds=8)
            page_title = driver.title or ""

            debug_path = save_debug_html(artist_url, html)
            if verbose:
                print("[DEBUG HTML]", debug_path)

            if detect_access_denied(html, page_title):
                report["blocked"] = True
                if verbose:
                    print("[ACCESS DENIED]", artist_url, "|", page_title)
                artist_reports.append(report)
                sleep_human(10.0, 20.0)
                continue

            parsed_items = parse_artist_page_prices(
                html=html,
                artist_url=artist_url,
                seen_ids=seen_ids,
                verbose=verbose,
            )

            if parsed_items:
                items.extend(parsed_items)
                report["found_in_artist"] = len(parsed_items)
            else:
                report["empty"] = True

            artist_reports.append(report)
            sleep_human(6.0, 12.0)

        finally:
            if driver is not None:
                try:
                    driver.quit()
                except Exception:
                    pass

    return items, artist_reports


# =========================================================
# Runner
# =========================================================

def run_price_import(limit_artists: int = 0, verbose: bool = False):
    prices, artist_reports = fetch_fansale_prices(limit_artists=limit_artists, verbose=verbose)

    stats = {
        "total": 0,
        "updated": 0,
        "not_in_db": 0,
        "price_not_found": 0,
        "no_performance": 0,
        "artists_total": len(artist_reports),
        "artists_blocked": 0,
        "artists_empty": 0,
        "artists_with_prices": 0,
    }

    for r in artist_reports:
        if r.get("blocked"):
            stats["artists_blocked"] += 1
        elif r.get("empty"):
            stats["artists_empty"] += 1
        elif r.get("found_in_artist", 0) > 0:
            stats["artists_with_prices"] += 1

    for item in prices:
        stats["total"] += 1
        result = update_single_price(item, verbose=verbose)
        if result in stats:
            stats[result] += 1

    stats["artist_reports"] = artist_reports
    return stats