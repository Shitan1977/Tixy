from __future__ import annotations

import os
import re
import time
import random
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import List, Optional, Dict, Tuple

from bs4 import BeautifulSoup
from django.core.management.base import BaseCommand
from django.utils import timezone
from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.firefox.service import Service

from api.models import EventoPiattaforma, Performance, Piattaforma


SEED_PRICES_FILE = "fansale_seed_prices.txt"


# =========================================================
# TEXT / URL HELPERS
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


def load_seed_artist_urls(filepath: str = SEED_PRICES_FILE) -> List[str]:
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


# =========================================================
# TIMING
# =========================================================

def sleep_human(min_seconds: float = 4.0, max_seconds: float = 8.0):
    time.sleep(random.uniform(min_seconds, max_seconds))


def sleep_after_block():
    time.sleep(random.uniform(20.0, 35.0))


# =========================================================
# SELENIUM
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


def accept_cookies_if_present(driver):
    try:
        buttons = driver.find_elements("tag name", "button")
        for b in buttons:
            txt = normalize_text(b.text).lower()
            if "accetta" in txt or "accept" in txt:
                try:
                    b.click()
                    time.sleep(2)
                    return True
                except Exception:
                    continue
    except Exception:
        pass
    return False


def warmup_homepage(driver):
    try:
        driver.get("https://www.fansale.it/")
        time.sleep(random.uniform(4.0, 7.0))
        accept_cookies_if_present(driver)
        time.sleep(random.uniform(2.0, 4.0))
    except Exception:
        pass


def fetch_html_with_driver(driver, url: str, wait_seconds: int = 10) -> str:
    driver.get(url)
    time.sleep(random.uniform(6.0, 9.0))

    try:
        accept_cookies_if_present(driver)
    except Exception:
        pass

    try:
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(random.uniform(3.0, 5.0))
        driver.execute_script("window.scrollTo(0, 0);")
        time.sleep(random.uniform(2.0, 4.0))
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


def save_debug_html(artist_url: str, html: str) -> str:
    debug_dir = Path("debug_fansale")
    debug_dir.mkdir(exist_ok=True)

    parts = artist_url.rstrip("/").split("/")
    slug = parts[-2] if len(parts) >= 2 else "unknown"
    artist_id = parts[-1] if len(parts) >= 1 else "unknown"

    filename = debug_dir / f"{slug}_{artist_id}.html"
    filename.write_text(html, encoding="utf-8")
    return str(filename)


# =========================================================
# BLOCK DETECTION
# =========================================================

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


# =========================================================
# PRICE PARSER
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


def extract_price_from_text(text: str) -> Tuple[Optional[str], Optional[Decimal]]:
    text = normalize_text(text)

    patterns = [
        r"prezzo\s+fisso\s*(?:€|eur)?\s*\d{1,3}(?:\.\d{3})*,\d{2}",
        r"offerte?\s+da\s*(?:€|eur)?\s*\d{1,3}(?:\.\d{3})*,\d{2}",
        r"\bda\s*(?:€|eur)?\s*\d{1,3}(?:\.\d{3})*,\d{2}",
        r"(?:€|eur)\s*\d{1,3}(?:\.\d{3})*,\d{2}",
        r"\d{1,3}(?:\.\d{3})*,\d{2}\s*(?:€|eur)",
    ]

    for pattern in patterns:
        m = re.search(pattern, text, flags=re.IGNORECASE)
        if m:
            price_text = normalize_text(m.group(0))
            price_value = parse_price_text_to_decimal(price_text)
            if price_value is not None:
                return price_text, price_value

    return None, None


# =========================================================
# PARSING ARTIST PAGE
# =========================================================

def get_link_context_text(a_tag, max_levels: int = 5) -> str:
    texts = []

    try:
        txt = normalize_text(a_tag.get_text(" ", strip=True))
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


def parse_artist_offers_with_prices(html: str, verbose: bool = False) -> List[Dict]:
    soup = BeautifulSoup(html, "html.parser")
    links = soup.find_all("a", href=True)

    results: List[Dict] = []
    seen_offer_ids = set()

    if verbose:
        print("[LINK COUNT]", len(links))

    for a in links:
        href = normalize_text(a.get("href", ""))

        if not re.match(r"^/tickets/all/[^/]+/\d+/\d+$", href):
            continue

        offer_id = href.rstrip("/").split("/")[-1]
        if offer_id in seen_offer_ids:
            continue

        context_text = get_link_context_text(a, max_levels=5)
        price_text, price_value = extract_price_from_text(context_text)

        results.append({
            "offer_id": offer_id,
            "offer_url": "https://www.fansale.it" + href,
            "context_text": context_text,
            "price_text": price_text,
            "price_value": price_value,
        })

        seen_offer_ids.add(offer_id)

        if verbose:
            print("[OFFER]", offer_id, "|", price_text, "|", price_value)

    return results


# =========================================================
# DB HELPERS
# =========================================================

def get_target_performance(evento) -> Optional[Performance]:
    return (
        Performance.objects
        .filter(evento=evento)
        .order_by("starts_at_utc")
        .first()
    )


def update_price_for_offer(
    *,
    piattaforma,
    offer_id: str,
    offer_url: str,
    price_text: Optional[str],
    price_value: Optional[Decimal],
    context_text: str,
    artist_url: str,
) -> str:
    ep = (
        EventoPiattaforma.objects
        .select_related("evento")
        .filter(
            piattaforma=piattaforma,
            id_evento_piattaforma=offer_id,
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
            "source_url": offer_url,
            "artist_url": artist_url,
            "offer_id": offer_id,
            "reason": "no_performance",
            "context_text": context_text[:1000],
        }
        ep.save(update_fields=["ultima_scansione", "snapshot_raw", "aggiornato_il"])
        return "no_performance"

    if price_value is None:
        ep.ultima_scansione = timezone.now()
        ep.snapshot_raw = {
            "fansale_price_found": False,
            "fansale_price_text": None,
            "fansale_price_value": None,
            "fansale_currency": None,
            "source_url": offer_url,
            "artist_url": artist_url,
            "offer_id": offer_id,
            "reason": "price_not_found_from_artist_context",
            "context_text": context_text[:1000],
        }
        ep.save(update_fields=["ultima_scansione", "snapshot_raw", "aggiornato_il"])
        return "price_not_found"

    perf.prezzo_min = price_value
    perf.prezzo_max = price_value
    perf.valuta = "EUR"
    perf.save(update_fields=["prezzo_min", "prezzo_max", "valuta", "aggiornato_il"])

    ep.ultima_scansione = timezone.now()
    ep.snapshot_raw = {
        "fansale_price_found": True,
        "fansale_price_text": price_text,
        "fansale_price_value": str(price_value),
        "fansale_currency": "EUR",
        "source_url": offer_url,
        "artist_url": artist_url,
        "offer_id": offer_id,
        "reason": "price_found_from_artist_context",
        "context_text": context_text[:1000],
    }
    ep.save(update_fields=["ultima_scansione", "snapshot_raw", "aggiornato_il"])
    return "updated"


# =========================================================
# COMMAND
# =========================================================

class Command(BaseCommand):
    help = "Aggiorna i prezzi fanSALE partendo dai seed artisti e matchando le offerte col DB"

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit-artists",
            type=int,
            default=5,
            help="Numero massimo di artisti seed da processare. 0 = tutti",
        )
        parser.add_argument(
            "--verbose",
            action="store_true",
            help="Log dettagliato",
        )

    def handle(self, *args, **options):
        limit_artists = options["limit_artists"]
        verbose = options["verbose"]

        try:
            piattaforma = Piattaforma.objects.get(nome="fansale")
        except Piattaforma.DoesNotExist:
            self.stdout.write(self.style.ERROR("Piattaforma fansale non trovata"))
            return

        artists = load_seed_artist_urls()
        if limit_artists > 0:
            artists = artists[:limit_artists]

        stats = {
            "artists_total": len(artists),
            "artists_ok": 0,
            "artists_blocked": 0,
            "offers_seen": 0,
            "updated": 0,
            "not_in_db": 0,
            "price_not_found": 0,
            "no_performance": 0,
            "errors": 0,
        }

        self.stdout.write(
            self.style.WARNING(
                f"[START] scan_fansale_prices_from_artists artists={len(artists)}"
            )
        )

        for idx, artist_url in enumerate(artists, start=1):
            if verbose:
                self.stdout.write(f"[ARTIST {idx}/{len(artists)}] {artist_url}")

            driver = None
            try:
                driver = build_driver()
                sleep_human(4.0, 7.0)
                warmup_homepage(driver)

                html = fetch_html_with_driver(driver, artist_url, wait_seconds=10)
                page_title = driver.title or ""

                debug_path = save_debug_html(artist_url, html)
                if verbose:
                    self.stdout.write(f"[DEBUG HTML] salvato in {debug_path}")

                if detect_access_denied(html, page_title):
                    stats["artists_blocked"] += 1
                    if verbose:
                        self.stdout.write(f"[BLOCKED] {artist_url} | {page_title}")
                    sleep_after_block()
                    continue

                offers = parse_artist_offers_with_prices(html, verbose=verbose)
                stats["offers_seen"] += len(offers)
                stats["artists_ok"] += 1

                for offer in offers:
                    result = update_price_for_offer(
                        piattaforma=piattaforma,
                        offer_id=offer["offer_id"],
                        offer_url=offer["offer_url"],
                        price_text=offer["price_text"],
                        price_value=offer["price_value"],
                        context_text=offer["context_text"],
                        artist_url=artist_url,
                    )

                    if result in stats:
                        stats[result] += 1

                    if verbose:
                        self.stdout.write(
                            f"[RESULT] offer_id={offer['offer_id']} result={result} price={offer['price_value']}"
                        )

                    sleep_human(1.5, 3.5)

                sleep_human(8.0, 15.0)

            except WebDriverException as e:
                stats["errors"] += 1
                if verbose:
                    self.stdout.write(f"[SELENIUM ERROR] {artist_url} err={str(e)}")
                sleep_after_block()

            except Exception as e:
                stats["errors"] += 1
                if verbose:
                    self.stdout.write(f"[ERROR] {artist_url} err={str(e)}")
                sleep_after_block()

            finally:
                if driver is not None:
                    try:
                        driver.quit()
                    except Exception:
                        pass

        self.stdout.write(
            self.style.SUCCESS(
                "[DONE] "
                f"artists_total={stats['artists_total']} "
                f"artists_ok={stats['artists_ok']} "
                f"artists_blocked={stats['artists_blocked']} "
                f"offers_seen={stats['offers_seen']} "
                f"updated={stats['updated']} "
                f"not_in_db={stats['not_in_db']} "
                f"price_not_found={stats['price_not_found']} "
                f"no_performance={stats['no_performance']} "
                f"errors={stats['errors']}"
            )
        )