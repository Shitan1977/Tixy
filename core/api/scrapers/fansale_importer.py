from __future__ import annotations

import hashlib
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional

import requests
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

def build_unique_slug(base_slug: str) -> str:
    """
    Restituisce uno slug libero.
    Se esiste già, aggiunge -2, -3, -4, ...
    """
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

def canonical_hash(title: str, city: str, starts_at: datetime) -> str:
    raw = f"{normalize_text(title)}|{normalize_text(city)}|{starts_at.isoformat()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def clean_venue_text(raw: str) -> str:
    raw = re.sub(r"\b\d{1,2}\.\d{2}\s+ore\b", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"\s+", " ", raw).strip()

    parts = raw.split()
    half = len(parts) // 2

    if half > 0 and len(parts) % 2 == 0 and parts[:half] == parts[half:]:
        raw = " ".join(parts[:half])

    return raw


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
    """
    Import robusto:
    - deduplica Evento tramite hash_canonico
    - fallback su slug se l'evento esiste già con altro hash
    - deduplica Performance tramite (evento, luogo, starts_at_utc)
    """
    artist = get_or_create_artist(item.title)
    luogo = get_or_create_location(item.venue_name, item.city, item.country_code)

    title_norm = normalize_text(item.title)
    base_slug = slugify_simple(f"{item.title}-{item.city}-{item.starts_at.date()}")
    hash_canonico = canonical_hash(item.title, item.city, item.starts_at)

    # 1) prima prova per hash
    evento = Evento.objects.filter(hash_canonico=hash_canonico).first()

    # 2) fallback per slug già esistente
    if not evento:
        evento = Evento.objects.filter(slug=base_slug[:255]).first()

    # 3) creazione evento solo se non trovato
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
        # eventuale allineamento dati mancanti
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

        # se mancava l'hash, lo valorizziamo
        if not evento.hash_canonico:
            evento.hash_canonico = hash_canonico
            changed = True

        if changed:
            evento.save()

    # 4) deduplica performance
    performance, performance_created = Performance.objects.get_or_create(
        evento=evento,
        luogo=luogo,
        starts_at_utc=item.starts_at,
        defaults={
            "status": "ONSALE",
        },
    )

    # 5) mapping piattaforma
    piattaforma, _ = Piattaforma.objects.get_or_create(
        nome="fansale",
        defaults={
            "dominio": "fansale.it",
            "attivo": True,
        },
    )

    EventoPiattaforma.objects.update_or_create(
        piattaforma=piattaforma,
        id_evento_piattaforma=item.external_id,
        defaults={
            "evento": evento,
            "url": item.event_url,
            "ultima_scansione": timezone.now(),
        },
    )

    if verbose:
        if performance_created:
            print(f"[CREATED] {item.title} | {item.city}")
        else:
            print(f"[SKIPPED EXISTS] {item.title} | {item.city}")

    return "created" if performance_created else "skipped_exists"

# =========================================================
# Selenium driver
# =========================================================

def get_firefox_binary():
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

    service = Service()

    driver = webdriver.Firefox(
        service=service,
        options=options,
    )

    driver.set_window_size(1600, 1200)
    return driver


# =========================================================
# Discovery artisti
# =========================================================

def fetch_html(url: str):
    headers = {
        "User-Agent": "Mozilla/5.0",
    }

    r = requests.get(url, headers=headers, timeout=20)
    r.raise_for_status()
    return r.text


def get_artist_urls(limit: int = 50) -> List[str]:
    pages = [
        "https://www.fansale.it/events/55",
        "https://www.fansale.it/events/new",
    ]

    urls = []
    seen = set()

    for page in pages:
        print("[DISCOVERY PAGE]", page)

        html = fetch_html(page)
        soup = BeautifulSoup(html, "html.parser")

        for a in soup.find_all("a", href=True):
            href = a["href"]

            if not re.match(r"^/tickets/all/[^/]+/\d+$", href):
                continue

            full = "https://www.fansale.it" + href

            if full in seen:
                continue

            seen.add(full)
            urls.append(full)

            if len(urls) >= limit:
                return urls

    return urls


# =========================================================
# Scraper eventi
# =========================================================

def fetch_fansale_events(limit: int = 100):
    items: List[FanSaleEventData] = []
    seen_ids = set()

    mesi = {
        "gen": 1, "feb": 2, "mar": 3, "apr": 4, "mag": 5, "giu": 6,
        "lug": 7, "ago": 8, "set": 9, "ott": 10, "nov": 11, "dic": 12,
    }

    def extract_artist_name(artist_url: str, page_title: str) -> str:
        if " su fanSALE" in page_title:
            return page_title.split(" su fanSALE")[0].strip()

        m = re.search(r"/tickets/all/([^/]+)/\d+", artist_url)
        if m:
            return m.group(1).replace("-", " ").title()

        return "Artista sconosciuto"

    artists = get_artist_urls()

    for artist_url in artists:
        artist_seen_for_pages = set()
        driver = build_driver()

        try:
            for page_num in range(1, 6):
                page_url = artist_url if page_num == 1 else f"{artist_url}#page-{page_num}"
                print("[ARTIST PAGE]", page_url)

                try:
                    driver.get(page_url)
                    time.sleep(5)

                    driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
                    time.sleep(2)
                    driver.execute_script("window.scrollTo(0, 0);")
                    time.sleep(1)

                    html = driver.page_source
                except WebDriverException:
                    continue

                page_title = driver.title or ""
                artist_name = extract_artist_name(artist_url, page_title)

                soup = BeautifulSoup(html, "html.parser")
                links = soup.find_all("a", href=True)

                print("[LINK COUNT]", page_num, len(links))

                found_in_this_page = 0

                for a in links:
                    href = a.get("href", "").strip()
                    text = a.get_text(" ", strip=True)

                    if not re.match(r"^/tickets/all/[^/]+/\d+/\d+$", href):
                        continue

                    if "Package:" in text:
                        continue

                    external_id = href.rstrip("/").split("/")[-1]

                    if external_id in seen_ids or external_id in artist_seen_for_pages:
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
                    )

                    seen_ids.add(external_id)
                    artist_seen_for_pages.add(external_id)
                    items.append(item)
                    found_in_this_page += 1

                    print("[PARSED]", item.title, "|", item.city, "|", item.venue_name, "|", item.starts_at)

                    if len(items) >= limit:
                        return items

                if page_num > 1 and found_in_this_page == 0:
                    break

        finally:
            try:
                driver.quit()
            except Exception:
                pass

    return items


# =========================================================
# Runner
# =========================================================

def run_import(limit: int = 100, verbose: bool = False):
    events = fetch_fansale_events(limit)

    stats = {
        "total": 0,
        "created": 0,
        "skipped_exists": 0,
        "skipped_not_it": 0,
    }

    for e in events:
        stats["total"] += 1

        result = import_single_event(e, verbose)

        if result in stats:
            stats[result] += 1

    return stats