import hashlib
import re
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Optional

from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify

from api.models import (
    Categoria,
    Evento,
    EventoPiattaforma,
    Luoghi,
    Performance,
    Piattaforma,
)

from .schemas import TicketOneEventItem


ROME_TZ = ZoneInfo("Europe/Rome")


def normalize_text(value: Optional[str]) -> str:
    if not value:
        return ""
    return " ".join(str(value).split()).strip()


def normalize_name(value: Optional[str]) -> str:
    value = normalize_text(value).lower()
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def build_unique_slug(base_text: str, suffix: Optional[str] = None) -> str:
    base = slugify(base_text)[:220] or "evento"
    if suffix:
        suffix_slug = slugify(str(suffix))[:30]
        return f"{base}-{suffix_slug}"[:255]
    return base[:255]


def parse_starts_at(raw_value: Optional[str]):
    raw_value = normalize_text(raw_value)
    if not raw_value:
        return None

    # formato attuale: 20/04/2026 20:30
    for fmt in ("%d/%m/%Y %H:%M", "%d/%m/%Y"):
        try:
            dt = datetime.strptime(raw_value, fmt)
            if fmt == "%d/%m/%Y":
                dt = dt.replace(hour=20, minute=0)
            aware = dt.replace(tzinfo=ROME_TZ)
            return aware.astimezone(ZoneInfo("UTC"))
        except ValueError:
            continue

    return None


def canonical_hash(title: str, city: str, starts_at_raw: str) -> str:
    payload = f"{normalize_name(title)}|{normalize_name(city)}|{normalize_text(starts_at_raw)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def get_or_create_categoria(category_hint: Optional[str]) -> Optional[Categoria]:
    category_hint = normalize_text(category_hint)
    if not category_hint:
        return None

    slug = slugify(category_hint)[:60]
    if not slug:
        return None

    categoria, _ = Categoria.objects.get_or_create(
        slug=slug,
        defaults={"nome": category_hint.title()}
    )
    return categoria


def get_or_create_luogo(item: TicketOneEventItem) -> Optional[Luoghi]:
    city = normalize_text(item.city)
    venue = normalize_text(item.venue)

    if not city and not venue:
        return None

    if venue:
        nome = venue
        nome_normalizzato = normalize_name(venue)
    else:
        nome = city
        nome_normalizzato = normalize_name(city)

    luogo, created = Luoghi.objects.get_or_create(
        nome_normalizzato=nome_normalizzato,
        defaults={
            "nome": nome,
            "citta": city or None,
            "citta_normalizzata": normalize_name(city) or None,
            "stato_iso": "IT",
            "timezone": "Europe/Rome",
        },
    )

    updated = False
    if city and luogo.citta != city:
        luogo.citta = city
        luogo.citta_normalizzata = normalize_name(city)
        updated = True

    if not luogo.stato_iso:
        luogo.stato_iso = "IT"
        updated = True

    if not luogo.timezone:
        luogo.timezone = "Europe/Rome"
        updated = True

    if updated:
        luogo.save(update_fields=["citta", "citta_normalizzata", "stato_iso", "timezone", "aggiornato_il"])

    return luogo


def get_or_create_evento(item: TicketOneEventItem, categoria: Optional[Categoria]) -> Evento:
    title = normalize_text(item.title) or "Evento TicketOne"
    city = normalize_text(item.city)
    starts_at_raw = normalize_text(item.starts_at_raw)

    hash_value = canonical_hash(title, city, starts_at_raw)
    slug = build_unique_slug(title, item.external_id or hash_value[:8])

    evento = Evento.objects.filter(hash_canonico=hash_value).first()
    if evento:
        updated = False
        if categoria and evento.categoria_id != categoria.id:
            evento.categoria = categoria
            updated = True
        if updated:
            evento.save(update_fields=["categoria", "aggiornato_il"])
        return evento

    evento = Evento.objects.create(
        slug=slug,
        nome_evento=title,
        nome_evento_normalizzato=normalize_name(title),
        stato="pianificato",
        categoria=categoria,
        hash_canonico=hash_value,
        note_raw={
            "source": item.source,
            "detail_status": item.detail_status,
            "ticketone_external_id": item.external_id,
        },
    )
    return evento


def get_or_create_performance(evento: Evento, luogo: Optional[Luoghi], item: TicketOneEventItem) -> Optional[Performance]:
    starts_at_utc = parse_starts_at(item.starts_at_raw)
    if not starts_at_utc or not luogo:
        return None

    performance = Performance.objects.filter(
        evento=evento,
        luogo=luogo,
        starts_at_utc=starts_at_utc,
    ).first()

    if performance:
        updated = False

        if item.price_text and performance.prezzo_min is None:
            # per ora non convertiamo il prezzo testuale
            pass

        if updated:
            performance.save()

        return performance

    return Performance.objects.create(
        evento=evento,
        luogo=luogo,
        starts_at_utc=starts_at_utc,
        status="ONSALE",
        disponibilita_agg="sconosciuta",
        valuta="EUR" if item.price_text else None,
    )


def get_or_create_piattaforma_ticketone() -> Piattaforma:
    piattaforma, _ = Piattaforma.objects.get_or_create(
        nome="ticketone",
        defaults={
            "dominio": "ticketone.it",
            "attivo": True,
        },
    )
    return piattaforma


@transaction.atomic
def import_ticketone_item(item: TicketOneEventItem) -> dict:
    piattaforma = get_or_create_piattaforma_ticketone()
    categoria = get_or_create_categoria(item.category_hint)
    luogo = get_or_create_luogo(item)
    evento = get_or_create_evento(item, categoria)
    performance = get_or_create_performance(evento, luogo, item)

    ep_defaults = {
        "url": item.event_url,
        "ultima_scansione": timezone.now(),
        "snapshot_raw": {
            "title": item.title,
            "city": item.city,
            "venue": item.venue,
            "starts_at_raw": item.starts_at_raw,
            "price_text": item.price_text,
            "detail_status": item.detail_status,
            "source": item.source,
        },
        "checksum_dati": hashlib.sha256(
            f"{item.title}|{item.city}|{item.venue}|{item.starts_at_raw}|{item.price_text}".encode("utf-8")
        ).hexdigest(),
    }

    if item.external_id:
        ep, created = EventoPiattaforma.objects.get_or_create(
            piattaforma=piattaforma,
            id_evento_piattaforma=item.external_id,
            defaults={
                "evento": evento,
                **ep_defaults,
            },
        )
        if not created:
            changed = False
            if ep.evento_id != evento.id:
                ep.evento = evento
                changed = True
            for field, value in ep_defaults.items():
                if getattr(ep, field) != value:
                    setattr(ep, field, value)
                    changed = True
            if changed:
                ep.save()
    else:
        ep, created = EventoPiattaforma.objects.get_or_create(
            evento=evento,
            piattaforma=piattaforma,
            defaults={
                "id_evento_piattaforma": None,
                **ep_defaults,
            },
        )
        if not created:
            changed = False
            for field, value in ep_defaults.items():
                if getattr(ep, field) != value:
                    setattr(ep, field, value)
                    changed = True
            if changed:
                ep.save()

    return {
        "evento_id": evento.id,
        "performance_id": performance.id if performance else None,
        "evento_piattaforma_id": ep.id,
        "created_event": evento.creato_il == evento.aggiornato_il,
        "has_performance": performance is not None,
        "detail_status": item.detail_status,
        "title": item.title,
    }