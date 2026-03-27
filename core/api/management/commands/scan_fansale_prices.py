from __future__ import annotations

import os
import re
import time
import random
from decimal import Decimal, InvalidOperation
from typing import Optional, List, Dict
from collections import defaultdict

from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.firefox.service import Service

from api.models import EventoPiattaforma, Performance, Piattaforma


# =========================================================
# HUMAN / TIMING
# =========================================================

def sleep_human(min_s: float = 2.5, max_s: float = 5.5):
    time.sleep(random.uniform(min_s, max_s))


def small_pause():
    sleep_human(1.0, 2.2)


def medium_pause():
    sleep_human(2.5, 4.5)


def long_pause():
    sleep_human(5.0, 8.0)


# =========================================================
# NORMALIZATION
# =========================================================

def normalize_space(text: str) -> str:
    text = text or ""
    text = text.replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text


# =========================================================
# URL HELPERS
# =========================================================

def extract_offer_id(url: str) -> str:
    return url.rstrip("/").split("/")[-1]


def extract_artist_url(url: str) -> str:
    """
    Da:
      https://www.fansale.it/tickets/all/geolier/577164/21304686
    a:
      https://www.fansale.it/tickets/all/geolier/577164
    """
    parts = url.rstrip("/").split("/")
    if len(parts) >= 7:
        return "/".join(parts[:7])
    return url


# =========================================================
# PRICE HELPERS
# =========================================================

def parse_price_text_to_decimal(price_text: str) -> Optional[Decimal]:
    if not price_text:
        return None

    txt = normalize_space(price_text).lower()
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


def extract_price_candidates(text: str) -> List[Decimal]:
    text = normalize_space(text)

    patterns = [
        r"prezzo\s+fisso\s*(?:€|eur)?\s*\d{1,3}(?:\.\d{3})*,\d{2}",
        r"offerte?\s+da\s*(?:€|eur)?\s*\d{1,3}(?:\.\d{3})*,\d{2}",
        r"\bda\s*(?:€|eur)?\s*\d{1,3}(?:\.\d{3})*,\d{2}",
        r"(?:€|eur)\s*\d{1,3}(?:\.\d{3})*,\d{2}",
        r"\d{1,3}(?:\.\d{3})*,\d{2}\s*(?:€|eur)",
    ]

    found_values: List[Decimal] = []
    seen = set()

    for pattern in patterns:
        for item in re.findall(pattern, text, flags=re.IGNORECASE):
            item_norm = normalize_space(item).lower()
            if item_norm in seen:
                continue
            seen.add(item_norm)

            value = parse_price_text_to_decimal(item)
            if value is not None:
                found_values.append(value)

    return found_values


def extract_best_price_from_text(text: str) -> Optional[Decimal]:
    values = extract_price_candidates(text)
    if not values:
        return None
    return min(values)


# =========================================================
# BLOCK / COOKIE DETECTION
# =========================================================

def detect_access_denied(text: str, title: str) -> bool:
    combined = f"{title} {text}".lower()
    signals = [
        "access denied",
        "you don't have permission",
        "forbidden",
        "errors.edgesuite.net",
    ]
    return any(s in combined for s in signals)


def detect_cookie_wall(text: str, title: str) -> bool:
    combined = f"{title} {text}".lower()
    return "cookie" in combined and ("consent" in combined or "privacy" in combined)


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
    driver.set_window_size(1366, 900)

    try:
        driver.execute_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
    except Exception:
        pass

    return driver


def accept_cookies(driver) -> bool:
    try:
        buttons = driver.find_elements(By.TAG_NAME, "button")
        for b in buttons:
            txt = normalize_space(b.text).lower()
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


def warmup_session(driver):
    driver.get("https://www.fansale.it/")
    time.sleep(5)
    accept_cookies(driver)
    medium_pause()


def fetch_artist_page(driver, artist_url: str, verbose: bool = False) -> Dict[str, str]:
    driver.get(artist_url)
    time.sleep(5)

    accept_cookies(driver)
    small_pause()

    try:
        driver.execute_script("window.scrollTo(0, 500);")
        time.sleep(1)
        driver.execute_script("window.scrollTo(0, 1200);")
        time.sleep(1)
        driver.execute_script("window.scrollTo(0, 0);")
        time.sleep(1)
    except Exception:
        pass

    title = driver.title or ""
    try:
        body_text = driver.find_element(By.TAG_NAME, "body").text
    except Exception:
        body_text = ""

    body_text = normalize_space(body_text)

    if verbose:
        print(f"[ARTIST TITLE] {title}")

    return {
        "title": title,
        "text": body_text,
    }


# =========================================================
# DOM EXTRACTION FROM ARTIST PAGE
# =========================================================

def get_offer_container_texts(driver, offer_id: str) -> List[str]:
    """
    Cerca il link della specifica offerta nella pagina artista
    e prova a leggere testo da:
    - link
    - parent
    - nonno
    - bisnonno
    """
    texts: List[str] = []

    xpath = f"//a[contains(@href,'/{offer_id}')]"
    links = driver.find_elements(By.XPATH, xpath)

    for link in links:
        # 1) testo del link
        try:
            txt = normalize_space(link.text)
            if txt:
                texts.append(txt)
        except Exception:
            pass

        # 2) testo di parent, nonno, bisnonno
        current = link
        for _ in range(4):
            try:
                current = current.find_element(By.XPATH, "./..")
                txt = normalize_space(current.text)
                if txt:
                    texts.append(txt)
            except Exception:
                break

        # 3) outerHTML per debug estremo
        try:
            html = link.get_attribute("outerHTML") or ""
            html = normalize_space(html)
            if html:
                texts.append(html)
        except Exception:
            pass

    # deduplica mantenendo ordine
    seen = set()
    ordered = []
    for t in texts:
        key = t[:500].lower()
        if key not in seen:
            seen.add(key)
            ordered.append(t)

    return ordered


def extract_offer_price_from_artist_page(driver, offer_id: str) -> Optional[Decimal]:
    candidate_texts = get_offer_container_texts(driver, offer_id)

    for txt in candidate_texts:
        value = extract_best_price_from_text(txt)
        if value is not None:
            return value

    return None


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


# =========================================================
# COMMAND
# =========================================================

class Command(BaseCommand):
    help = "Aggiorna i prezzi fanSALE usando solo le pagine artista, in modo lento e umano"

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit",
            type=int,
            default=10,
            help="Numero massimo di record da processare",
        )
        parser.add_argument(
            "--verbose",
            action="store_true",
            help="Log dettagliato",
        )

    def handle(self, *args, **options):
        limit = options["limit"]
        verbose = options["verbose"]

        try:
            piattaforma = Piattaforma.objects.get(nome="fansale")
        except Piattaforma.DoesNotExist:
            self.stdout.write(self.style.ERROR("Piattaforma fansale non trovata"))
            return

        qs = (
            EventoPiattaforma.objects
            .filter(piattaforma=piattaforma)
            .exclude(url="")
            .filter(
                Q(snapshot_raw__isnull=True) |
                Q(snapshot_raw__fansale_price_found=False)
            )
            .select_related("evento")
            .order_by("ultima_scansione")[:limit]
        )

        total = qs.count()
        updated = 0
        skipped = 0
        errors = 0

        self.stdout.write(
            self.style.WARNING(
                f"[START] scan_fansale_prices limit={limit} found={total}"
            )
        )

        # raggruppa per artist_url: una pagina artista, più offerte
        grouped: Dict[str, List[EventoPiattaforma]] = defaultdict(list)
        for ep in qs:
            artist_url = extract_artist_url(ep.url)
            grouped[artist_url].append(ep)

        driver = None
        try:
            driver = build_driver()
            warmup_session(driver)

            for artist_url, artist_eps in grouped.items():
                if verbose:
                    self.stdout.write(f"[ARTIST PAGE] {artist_url}")

                try:
                    page_data = fetch_artist_page(driver, artist_url, verbose=False)
                    page_title = page_data.get("title", "")
                    page_text = page_data.get("text", "")

                    if detect_access_denied(page_text, page_title):
                        for ep in artist_eps:
                            ep.ultima_scansione = timezone.now()
                            ep.snapshot_raw = {
                                "fansale_price_found": False,
                                "fansale_price_text": None,
                                "fansale_price_value": None,
                                "fansale_currency": None,
                                "source_url": ep.url,
                                "artist_url": artist_url,
                                "reason": "access_denied_artist_page",
                                "page_title": page_title,
                                "text_sample": page_text[:1000],
                            }
                            ep.save(update_fields=["ultima_scansione", "snapshot_raw", "aggiornato_il"])
                            skipped += 1

                            if verbose:
                                self.stdout.write(f"[SKIP] access denied | evento={ep.evento_id} | url={ep.url}")

                        long_pause()
                        continue

                    if detect_cookie_wall(page_text, page_title):
                        accept_cookies(driver)
                        medium_pause()
                        page_data = fetch_artist_page(driver, artist_url, verbose=False)
                        page_title = page_data.get("title", "")
                        page_text = page_data.get("text", "")

                    for ep in artist_eps:
                        evento = ep.evento
                        url = ep.url
                        offer_id = extract_offer_id(url)

                        perf = get_target_performance(evento)
                        if not perf:
                            skipped += 1
                            ep.ultima_scansione = timezone.now()
                            ep.snapshot_raw = {
                                "fansale_price_found": False,
                                "fansale_price_text": None,
                                "fansale_price_value": None,
                                "fansale_currency": None,
                                "source_url": url,
                                "artist_url": artist_url,
                                "reason": "no_performance",
                            }
                            ep.save(update_fields=["ultima_scansione", "snapshot_raw", "aggiornato_il"])

                            if verbose:
                                self.stdout.write(f"[SKIP] nessuna performance | evento={evento.id}")
                            continue

                        try:
                            price_value = extract_offer_price_from_artist_page(driver, offer_id)

                            if price_value is None:
                                skipped += 1
                                ep.ultima_scansione = timezone.now()
                                ep.snapshot_raw = {
                                    "fansale_price_found": False,
                                    "fansale_price_text": None,
                                    "fansale_price_value": None,
                                    "fansale_currency": None,
                                    "source_url": url,
                                    "artist_url": artist_url,
                                    "offer_id": offer_id,
                                    "reason": "price_not_found_in_artist_page",
                                    "page_title": page_title,
                                    "text_sample": page_text[:1000],
                                }
                                ep.save(update_fields=["ultima_scansione", "snapshot_raw", "aggiornato_il"])

                                if verbose:
                                    self.stdout.write(
                                        f"[NO PRICE] evento={evento.id} offer_id={offer_id} url={url}"
                                    )
                                continue

                            perf.prezzo_min = price_value
                            perf.prezzo_max = price_value
                            perf.valuta = "EUR"
                            perf.save(update_fields=["prezzo_min", "prezzo_max", "valuta", "aggiornato_il"])

                            ep.ultima_scansione = timezone.now()
                            ep.snapshot_raw = {
                                "fansale_price_found": True,
                                "fansale_price_text": str(price_value),
                                "fansale_price_value": str(price_value),
                                "fansale_currency": "EUR",
                                "source_url": url,
                                "artist_url": artist_url,
                                "offer_id": offer_id,
                                "reason": "price_found_from_artist_page",
                                "page_title": page_title,
                            }
                            ep.save(update_fields=["ultima_scansione", "snapshot_raw", "aggiornato_il"])

                            updated += 1

                            if verbose:
                                self.stdout.write(
                                    f"[UPDATED] evento={evento.id} perf={perf.id} prezzo={price_value} EUR"
                                )

                        except Exception as inner_e:
                            errors += 1
                            ep.ultima_scansione = timezone.now()
                            ep.snapshot_raw = {
                                "fansale_price_found": False,
                                "fansale_price_text": None,
                                "fansale_price_value": None,
                                "fansale_currency": None,
                                "source_url": url,
                                "artist_url": artist_url,
                                "offer_id": offer_id,
                                "reason": "inner_offer_parse_error",
                                "error": str(inner_e)[:500],
                            }
                            ep.save(update_fields=["ultima_scansione", "snapshot_raw", "aggiornato_il"])

                            if verbose:
                                self.stdout.write(
                                    f"[ERROR] evento={evento.id} offer_id={offer_id} err={str(inner_e)}"
                                )

                    # pausa tra artisti
                    medium_pause()

                except WebDriverException as e:
                    errors += len(artist_eps)
                    for ep in artist_eps:
                        ep.ultima_scansione = timezone.now()
                        ep.snapshot_raw = {
                            "fansale_price_found": False,
                            "fansale_price_text": None,
                            "fansale_price_value": None,
                            "fansale_currency": None,
                            "source_url": ep.url,
                            "artist_url": artist_url,
                            "reason": "selenium_artist_page_error",
                            "error": str(e)[:500],
                        }
                        ep.save(update_fields=["ultima_scansione", "snapshot_raw", "aggiornato_il"])

                    if verbose:
                        self.stdout.write(f"[SELENIUM ERROR] artist_url={artist_url} err={str(e)}")

                    long_pause()

                except Exception as e:
                    errors += len(artist_eps)
                    for ep in artist_eps:
                        ep.ultima_scansione = timezone.now()
                        ep.snapshot_raw = {
                            "fansale_price_found": False,
                            "fansale_price_text": None,
                            "fansale_price_value": None,
                            "fansale_currency": None,
                            "source_url": ep.url,
                            "artist_url": artist_url,
                            "reason": "generic_artist_page_error",
                            "error": str(e)[:500],
                        }
                        ep.save(update_fields=["ultima_scansione", "snapshot_raw", "aggiornato_il"])

                    if verbose:
                        self.stdout.write(f"[ERROR] artist_url={artist_url} err={str(e)}")

                    long_pause()

        finally:
            if driver is not None:
                try:
                    driver.quit()
                except Exception:
                    pass

        self.stdout.write(
            self.style.SUCCESS(
                f"[DONE] total={total} updated={updated} skipped={skipped} errors={errors}"
            )
        )