from __future__ import annotations
import random
import hashlib
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional, Tuple

from bs4 import BeautifulSoup
from django.utils import timezone
from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.firefox.service import Service

from api.models import (
    Artista,
    Evento,
    EventoPiattaforma,
    Luoghi,
    Performance,
    Piattaforma,
)


SEED_FILE = "fansale_seed_artists.txt"


# =========================================================
# Helpers
# =========================================================

def normalize_text(value: Optional[str]) -> str:
    if not value:
        return ""
    value = value.strip().lower()
    value = re.sub(r"\s+", " ", value)
    return value


def slugify_simple(value: str) -> str:
    value = normalize_text(value)
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-")


def normalize_artist_url(url: str) -> str:
    return url.split("#")[0].strip()


def is_valid_artist_url(url: str) -> bool:
    return re.fullmatch(r"https://www\.fansale\.it/tickets/all/[^/]+/\d+", url) is not None


def clean_venue_text(raw: str) -> str:
    raw = re.sub(r"\b\d{1,2}\.\d{2}\s+ore\b", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"\s+", " ", raw).strip()

    parts = raw.split()
    half = len(parts) // 2

    if half > 0 and len(parts) % 2 == 0 and parts[:half] == parts[half:]:
        raw = " ".join(parts[:half])

    return raw


def canonical_hash(title: str, city: str, starts_at: datetime) -> str:
    raw = f"{normalize_text(title)}|{normalize_text(city)}|{starts_at.isoformat()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def build_unique_slug(base_slug: str) -> str:
    slug = base_slug[:255]

    if not Evento.objects.filter(slug=slug).exists():
        return slug

    counter = 2
    while True:
        suffix = f"-{counter}"
        candidate = f"{base_slug[:255 - len(suffix)]}{suffix}"
        if not Evento.objects.filter(slug=candidate).exists():
            return candidate
        counter += 1


def extract_artist_name(artist_url: str, page_title: str) -> str:
    if " su fanSALE" in page_title:
        return page_title.split(" su fanSALE")[0].strip()

    m = re.search(r"/tickets/all/([^/]+)/\d+$", artist_url)
    if m:
        return m.group(1).replace("-", " ").title()

    return "Artista sconosciuto"


def detect_access_denied(html: str, title: str) -> bool:
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True).lower()
    title_l = (title or "").strip().lower()

    signals = [
        "access denied",
        "you don't have permission",
        "forbidden",
        "blocked",
    ]

    if any(s in title_l for s in signals):
        return True

    if any(s in text for s in signals):
        return True

    return False


def load_seed_artist_urls(filepath: str = SEED_FILE) -> List[str]:
    urls = []

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

    # deduplica mantenendo ordine
    seen = set()
    ordered = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            ordered.append(u)

    return ordered


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

    # fingerprint un po' meno "vuoto"
    options.set_preference(
        "general.useragent.override",
        "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"
    )
    options.set_preference("dom.webdriver.enabled", False)
    options.set_preference("useAutomationExtension", False)
    options.set_preference("media.peerconnection.enabled", False)

    service = Service()

    driver = webdriver.Firefox(
        service=service,
        options=options,
    )
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
    while time.time() < end_time:
        html = driver.page_source
        if "/tickets/all/" in html:
            return html
        time.sleep(1)

    return driver.page_source

def sleep_human(min_seconds: float = 2.0, max_seconds: float = 5.0):
    time.sleep(random.uniform(min_seconds, max_seconds))

# =========================================================
# Event dataclass
# =========================================================

@dataclass
class FanSaleEventData:
    external_id: str
    title: str
    venue_name: str
    city: str
    country_code: str
    event_url: str
    image_url: Optional[str]
    starts_at: datetime
    artist_url: Optional[str] = None


# =========================================================
# DB helpers
# =========================================================

def get_or_create_artist(title: str) -> Optional[Artista]:
    name_norm = normalize_text(title)
    artist = Artista.objects.filter(nome_normalizzato=name_norm).first()

    if artist:
        return artist

    return Artista.objects.create(
        nome=title[:255],
        nome_normalizzato=name_norm,
        tipo="artista",
    )


def get_or_create_location(venue_name: str, city: str, country_code: str) -> Luoghi:
    venue_norm = normalize_text(venue_name)

    luogo = Luoghi.objects.filter(nome_normalizzato=venue_norm).first()
    if luogo:
        changed = False
        city_norm = normalize_text(city)

        if city and luogo.citta != city:
            luogo.citta = city
            luogo.citta_normalizzata = city_norm
            changed = True

        if country_code and luogo.stato_iso != country_code:
            luogo.stato_iso = country_code
            changed = True

        if changed:
            luogo.save(update_fields=["citta", "citta_normalizzata", "stato_iso", "aggiornato_il"])

        return luogo

    return Luoghi.objects.create(
        nome=venue_name,
        nome_normalizzato=venue_norm,
        citta=city,
        citta_normalizzata=normalize_text(city),
        stato_iso=country_code,
    )


def import_single_event(item: FanSaleEventData, verbose: bool = False) -> str:
    artist = get_or_create_artist(item.title)
    luogo = get_or_create_location(item.venue_name, item.city, item.country_code)

    title_norm = normalize_text(item.title)
    base_slug = slugify_simple(f"{item.title}-{item.city}-{item.starts_at.date()}")
    hash_canonico = canonical_hash(item.title, item.city, item.starts_at)

    evento = Evento.objects.filter(hash_canonico=hash_canonico).first()

    if not evento:
        evento = Evento.objects.filter(slug=base_slug[:255]).first()

    if not evento:
        slug = build_unique_slug(base_slug)

        evento = Evento.objects.create(
            slug=slug,
            nome_evento=item.title[:255],
            nome_evento_normalizzato=title_norm[:255],
            immagine_url=item.image_url,
            artista_principale=artist,
            hash_canonico=hash_canonico,
        )
    else:
        changed = False

        if not evento.nome_evento:
            evento.nome_evento = item.title[:255]
            changed = True

        if not evento.nome_evento_normalizzato:
            evento.nome_evento_normalizzato = title_norm[:255]
            changed = True

        if not evento.artista_principale_id and artist:
            evento.artista_principale = artist
            changed = True

        if not evento.immagine_url and item.image_url:
            evento.immagine_url = item.image_url
            changed = True

        if not evento.hash_canonico:
            evento.hash_canonico = hash_canonico
            changed = True

        if changed:
            evento.save()

    performance, performance_created = Performance.objects.get_or_create(
        evento=evento,
        luogo=luogo,
        starts_at_utc=item.starts_at,
        defaults={
            "status": "ONSALE",
        },
    )

    piattaforma, _ = Piattaforma.objects.get_or_create(
        nome="fansale",
        defaults={
            "dominio": "fansale.it",
            "attivo": True,
        },
    )

    ep_defaults = {
        "id_evento_piattaforma": item.external_id,
        "url": item.event_url,
        "ultima_scansione": timezone.now(),
    }

    ep_obj = EventoPiattaforma.objects.filter(
        evento=evento,
        piattaforma=piattaforma,
    ).first()

    if ep_obj:
        changed = False

        if ep_obj.id_evento_piattaforma != item.external_sid:
            ep_obj.id_evento_piattaforma = item.external_id
            changed = True

        if ep_obj.url != item.event_url:
            ep_obj.url = item.event_url
            changed = True

        ep_obj.ultima_scansione = timezone.now()
        changed = True

        if changed:
            ep_obj.save(update_fields=["id_evento_piattaforma", "url", "ultima_scansione"])
    else:
        EventoPiattaforma.objects.create(
            evento=evento,
            piattaforma=piattaforma,
            **ep_defaults,
        )

    if verbose:
        if performance_created:
            print(f"[CREATED] {item.title} | {item.city}")
        else:
            print(f"[SKIPPED EXISTS] {item.title} | {item.city}")

    return "created" if performance_created else "skipped_exists"


# =========================================================
# Parsing artist page
# =========================================================

def parse_artist_page(
    *,
    html: str,
    artist_url: str,
    page_title: str,
    seen_ids: set,
    verbose: bool = False,
) -> List[FanSaleEventData]:
    items: List[FanSaleEventData] = []

    mesi = {
        "gen": 1, "feb": 2, "mar": 3, "apr": 4, "mag": 5, "giu": 6,
        "lug": 7, "ago": 8, "set": 9, "ott": 10, "nov": 11, "dic": 12,
    }

    artist_name = extract_artist_name(artist_url, page_title)
    soup = BeautifulSoup(html, "html.parser")
    links = soup.find_all("a", href=True)

    if verbose:
        print("[LINK COUNT]", len(links))

    for a in links:
        href = a.get("href", "").strip()
        text = a.get_text(" ", strip=True)

        # SOLO link offerta/evento
        if not re.match(r"^/tickets/all/[^/]+/\d+/\d+$", href):
            continue

        if "Package:" in text:
            continue

        external_id = href.rstrip("/").split("/")[-1]

        if external_id in seen_ids:
            continue

        text_clean = re.sub(r"\s+", " ", text).strip()

        date_match = re.search(
            r"(\d{1,2})\.\s*([a-z]{3})\s*(\d{2})",
            text_clean,
            re.IGNORECASE,
        )

        city_match = re.search(
            rf"{re.escape(artist_name)}\s+([A-ZÀ-Ù' \-]+?)\s+Offerte da",
            text_clean,
            re.IGNORECASE,
        )

        time_match = re.search(
            r"(\d{1,2}\.\d{2})\s+ore,",
            text_clean,
            re.IGNORECASE,
        )

        if not date_match or not city_match or not time_match:
            continue

        day = int(date_match.group(1))
        month_txt = date_match.group(2).lower()
        year_2 = int(date_match.group(3))

        city = city_match.group(1).strip().title()
        time_txt = time_match.group(1).strip()

        month = mesi.get(month_txt)
        if not month:
            continue

        after_time = re.split(
            r"\d{1,2}\.\d{2}\s+ore,\s*",
            text_clean,
            maxsplit=1,
            flags=re.IGNORECASE,
        )
        if len(after_time) <= 1:
            continue

        venue_part = after_time[1]
        venue = re.split(
            r"\s+Offerte da",
            venue_part,
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0].strip()

        venue = clean_venue_text(venue)
        if not venue:
            continue

        year = 2000 + year_2
        hour, minute = map(int, time_txt.split("."))

        starts_at = timezone.datetime(
            year,
            month,
            day,
            hour,
            minute,
            tzinfo=timezone.get_current_timezone(),
        )

        item = FanSaleEventData(
            external_id=external_id,
            title=artist_name,
            venue_name=venue,
            city=city,
            country_code="IT",
            event_url="https://www.fansale.it" + href,
            image_url=None,
            starts_at=starts_at,
            artist_url=artist_url,
        )

        seen_ids.add(external_id)
        items.append(item)

        if verbose:
            print("[PARSED]", item.title, "|", item.city, "|", item.venue_name, "|", item.starts_at)

    return items


# =========================================================
# Scraper events from seed only
# =========================================================

def fetch_fansale_events(limit: int = 0, verbose: bool = False) -> Tuple[List[FanSaleEventData], List[dict]]:
    items: List[FanSaleEventData] = []
    artist_reports: List[dict] = []
    seen_ids = set()

    artists = load_seed_artist_urls()

    print("[SEED ARTIST URLS TOTAL]", len(artists))

    if not artists:
        return items, artist_reports

    for idx, artist_url in enumerate(artists, start=1):
        report = {
            "artist_url": artist_url,
            "pages_scanned": 0,
            "found_in_artist": 0,
            "created": 0,
            "skipped_exists": 0,
            "blocked": False,
            "empty": False,
        }

        if verbose:
            print(f"[ARTIST {idx}/{len(artists)}] {artist_url}")

        driver = None
        try:
            driver = build_driver()

            # piccola pausa prima di iniziare l'artista
            sleep_human(1.5, 3.5)

            for page_num in range(1, 6):
                page_url = artist_url if page_num == 1 else f"{artist_url}#page-{page_num}"

                if verbose:
                    print("[ARTIST PAGE]", page_url)

                try:
                    html = fetch_html_with_driver(driver, page_url, wait_seconds=8)
                except WebDriverException as e:
                    report["blocked"] = True
                    if verbose:
                        print("[ARTIST ERROR]", artist_url, str(e))
                    break
                except Exception as e:
                    report["blocked"] = True
                    if verbose:
                        print("[ARTIST ERROR]", artist_url, str(e))
                    break

                report["pages_scanned"] += 1
                page_title = driver.title or ""

                if detect_access_denied(html, page_title):
                    report["blocked"] = True
                    if verbose:
                        print("[ACCESS DENIED]", artist_url, "|", page_title)
                    break

                parsed_items = parse_artist_page(
                    html=html,
                    artist_url=artist_url,
                    page_title=page_title,
                    seen_ids=seen_ids,
                    verbose=verbose,
                )

                if parsed_items:
                    items.extend(parsed_items)
                    report["found_in_artist"] += len(parsed_items)

                # se da pagina 2 in poi non trova niente, si ferma
                if page_num > 1 and len(parsed_items) == 0:
                    break

                # piccola pausa tra le pagine dello stesso artista
                sleep_human(1.0, 2.5)

                if limit and len(items) >= limit:
                    artist_reports.append(report)
                    return items[:limit], artist_reports

        finally:
            if driver is not None:
                try:
                    driver.quit()
                except Exception:
                    pass

        if report["found_in_artist"] == 0 and not report["blocked"]:
            report["empty"] = True

        artist_reports.append(report)

        # pausa tra artisti
        sleep_human(2.5, 5.5)

    return items, artist_reports


# =========================================================
# Runner
# =========================================================

def run_import(limit: int = 0, verbose: bool = False):
    events, artist_reports = fetch_fansale_events(limit=limit, verbose=verbose)

    stats = {
        "total": 0,
        "created": 0,
        "skipped_exists": 0,
        "skipped_not_it": 0,
        "artists_total": len(artist_reports),
        "artists_with_events": 0,
        "artists_blocked": 0,
        "artists_empty": 0,
        "artist_reports": artist_reports,
    }

    artist_index = {r["artist_url"]: r for r in artist_reports}

    for r in artist_reports:
        if r.get("blocked"):
            stats["artists_blocked"] += 1
        elif r.get("empty"):
            stats["artists_empty"] += 1
        elif r.get("found_in_artist", 0) > 0:
            stats["artists_with_events"] += 1

    for e in events:
        stats["total"] += 1

        result = import_single_event(e, verbose=verbose)

        if result in stats:
            stats[result] += 1

        if e.artist_url and e.artist_url in artist_index:
            artist_index[e.artist_url][result] = artist_index[e.artist_url].get(result, 0) + 1

    return stats