from __future__ import annotations

import re
import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, List

from django.utils import timezone

from api.models import (
    Artista,
    Luoghi,
    Evento,
    Performance,
    Piattaforma,
    EventoPiattaforma,
)


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


def is_italy_event(item: FanSaleEventData) -> bool:
    return (item.country_code or "").upper() == "IT"


def canonical_hash(title: str, city: str) -> str:
    raw = f"{normalize_text(title)}|{normalize_text(city)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def get_or_create_artist(title: str) -> Optional[Artista]:
    name_norm = normalize_text(title)
    if not name_norm:
        return None

    artist = Artista.objects.filter(nome_normalizzato=name_norm).first()
    if artist:
        return artist

    return Artista.objects.create(
        nome=title[:255],
        nome_normalizzato=name_norm,
        tipo="artista",
    )


def get_or_create_location(venue_name: str, city: str, country_code: str) -> Luoghi:
    venue_norm = normalize_text(venue_name) or normalize_text(city) or "venue-sconosciuta"
    city_norm = normalize_text(city)

    luogo = Luoghi.objects.filter(nome_normalizzato=venue_norm).first()
    if luogo:
        changed = False
        if city and luogo.citta != city:
            luogo.citta = city
            luogo.citta_normalizzata = city_norm
            changed = True
        if country_code and luogo.stato_iso != country_code.upper():
            luogo.stato_iso = country_code.upper()
            changed = True
        if changed:
            luogo.save(update_fields=["citta", "citta_normalizzata", "stato_iso", "aggiornato_il"])
        return luogo

    return Luoghi.objects.create(
        nome=venue_name or city or "Venue sconosciuta",
        nome_normalizzato=venue_norm,
        citta=city or None,
        citta_normalizzata=city_norm or None,
        stato_iso=(country_code or "").upper() or None,
    )


def event_already_exists(title: str, city: str, starts_at: datetime) -> bool:
    title_norm = normalize_text(title)
    city_norm = normalize_text(city)

    same_name = Evento.objects.filter(nome_evento_normalizzato=title_norm)

    for evento in same_name:
        perf_exists = evento.performances.filter(
            luogo__citta_normalizzata=city_norm,
            starts_at_utc=starts_at,
        ).exists()
        if perf_exists:
            return True
    return False


def import_single_event(item: FanSaleEventData, verbose: bool = False) -> str:
    if not is_italy_event(item):
        return "skipped_not_it"

    if event_already_exists(item.title, item.city, item.starts_at):
        return "skipped_exists"

    artist = get_or_create_artist(item.title)
    luogo = get_or_create_location(item.venue_name, item.city, item.country_code)

    title_norm = normalize_text(item.title)
    slug = slugify_simple(f"{item.title}-{item.city}-{item.starts_at.date()}")
    hash_canonico = canonical_hash(item.title, item.city)

    evento = Evento.objects.create(
        slug=slug[:255],
        nome_evento=item.title[:255],
        nome_evento_normalizzato=title_norm[:255],
        immagine_url=item.image_url,
        artista_principale=artist,
        hash_canonico=hash_canonico,
        note_raw={
            "source": "fansale",
            "external_id": item.external_id,
            "event_url": item.event_url,
        },
    )

    performance = Performance.objects.create(
        evento=evento,
        luogo=luogo,
        starts_at_utc=item.starts_at,
        status="ONSALE",
        disponibilita_agg="sconosciuta",
    )

    piattaforma, _ = Piattaforma.objects.get_or_create(
        nome="fansale",
        defaults={"dominio": "fansale.it", "attivo": True},
    )

    EventoPiattaforma.objects.update_or_create(
        piattaforma=piattaforma,
        id_evento_piattaforma=item.external_id,
        defaults={
            "evento": evento,
            "url": item.event_url,
            "ultima_scansione": timezone.now(),
            "snapshot_raw": {
                "title": item.title,
                "venue_name": item.venue_name,
                "city": item.city,
                "country_code": item.country_code,
                "starts_at": item.starts_at.isoformat(),
                "image_url": item.image_url,
            },
        },
    )

    if verbose:
        print(f"[CREATED] evento={evento.nome_evento} perf={performance.id}")

    return "created"


def fetch_fansale_events(limit: int = 100) -> List[FanSaleEventData]:
    """
    Stub locale iniziale.
    Qui per ora mettiamo dati finti di test, poi lo colleghiamo
    al parser reale fanSALE.
    """
    sample = [
        FanSaleEventData(
            external_id="fansale-test-001",
            title="Coldplay",
            venue_name="Stadio Olimpico",
            city="Roma",
            country_code="IT",
            event_url="https://www.fansale.it/",
            image_url=None,
            starts_at=timezone.now().replace(hour=20, minute=0, second=0, microsecond=0),
        ),
    ]
    return sample[:limit]


def run_import(limit: int = 100, verbose: bool = False) -> dict:
    items = fetch_fansale_events(limit=limit)

    stats = {
        "total": 0,
        "created": 0,
        "skipped_exists": 0,
        "skipped_not_it": 0,
    }

    for item in items:
        stats["total"] += 1
        result = import_single_event(item, verbose=verbose)
        if result in stats:
            stats[result] += 1

    return stats