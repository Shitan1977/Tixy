# api/scrapers/vivaticket/importer.py
from datetime import timezone as datetime_timezone
import hashlib
import re
from datetime import datetime, timezone as datetime_timezone
from zoneinfo import ZoneInfo

from django.utils import timezone
from django.utils.text import slugify

from api.models import (
    Categoria,
    Evento,
    EventoPiattaforma,
    Luoghi,
    Performance,
    PerformancePiattaforma,
    Piattaforma,
)


ITALY_TZ = ZoneInfo("Europe/Rome")


def normalize_text(value: str | None) -> str:
    """
    Normalizza testo per confronti semplici.
    """

    if not value:
        return ""

    value = value.strip().lower()
    value = re.sub(r"\s+", " ", value)

    return value


def build_unique_slug(base_text: str, external_id: str | None = None) -> str:
    """
    Crea uno slug stabile.
    Se abbiamo external_id, lo appendiamo per evitare collisioni.
    """

    base_slug = slugify(base_text or "evento")

    if not base_slug:
        base_slug = "evento"

    if external_id:
        return f"{base_slug}-{external_id}"

    return base_slug


def canonical_hash(title: str, city: str | None, starts_at_raw: str | None) -> str:
    """
    Hash canonico per evitare duplicati evento.
    """

    raw = f"{normalize_text(title)}|{normalize_text(city)}|{starts_at_raw or ''}"

    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def parse_vivaticket_datetime(value: str | None):
    """
    Converte datetime Vivaticket in datetime timezone-aware.

    Esempio:
    2026-07-03T21:00:00
    """

    if not value:
        return None

    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None

    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, ITALY_TZ)

    return dt.astimezone(datetime_timezone.utc)


def map_performance_status(sale_status: str | None) -> str:
    """
    Mappa sale_status Vivaticket verso Performance.status.
    """

    if sale_status in ["sold_out"]:
        return "SOLD_OUT"

    if sale_status in ["not_available", "inactive_sell_button", "no_sell_button"]:
        return "ONSALE"

    return "ONSALE"


def map_availability(sale_status: str | None) -> str:
    """
    Mappa sale_status Vivaticket verso Performance.disponibilita_agg.
    """

    if sale_status in ["available", "available_or_special"]:
        return "disponibile"

    if sale_status == "sold_out":
        return "non_disponibile"

    if sale_status in ["not_available", "inactive_sell_button", "no_sell_button"]:
        return "non_disponibile"

    return "sconosciuta"


def get_or_create_vivaticket_platform():
    """
    Recupera o crea la piattaforma Vivaticket.
    """

    piattaforma, _ = Piattaforma.objects.get_or_create(
        nome="vivaticket",
        defaults={
            "dominio": "vivaticket.com",
            "attivo": True,
        },
    )

    return piattaforma


def get_or_create_music_category():
    """
    Recupera o crea categoria musica.
    """

    categoria, _ = Categoria.objects.get_or_create(
        slug="musica",
        defaults={
            "nome": "Musica",
        },
    )

    return categoria


def get_or_create_place(event_data: dict):
    """
    Crea o aggiorna il luogo.
    """

    venue = event_data.get("venue") or "Luogo non specificato"
    city = event_data.get("city") or ""
    address = event_data.get("address") or ""

    nome_normalizzato = normalize_text(venue)
    citta_normalizzata = normalize_text(city)

    luogo, created = Luoghi.objects.get_or_create(
        nome_normalizzato=nome_normalizzato,
        defaults={
            "nome": venue,
            "indirizzo": address,
            "citta": city,
            "citta_normalizzata": citta_normalizzata,
            "stato_iso": "IT",
        },
    )

    changed = False

    if venue and luogo.nome != venue:
        luogo.nome = venue
        changed = True

    if address and luogo.indirizzo != address:
        luogo.indirizzo = address
        changed = True

    if city and luogo.citta != city:
        luogo.citta = city
        luogo.citta_normalizzata = citta_normalizzata
        changed = True

    if not luogo.stato_iso:
        luogo.stato_iso = "IT"
        changed = True

    if changed:
        luogo.save()

    return luogo, created


def get_or_create_event(event_data: dict, external_id: str):
    """
    Crea o aggiorna Evento.

    Prima prova a recuperare l'evento tramite EventoPiattaforma Vivaticket.
    Questo evita duplicati se Vivaticket cambia titolo o formattazione.
    """

    title = event_data.get("title") or "Evento senza titolo"
    city = event_data.get("city")
    starts_at_raw = event_data.get("starts_at_raw")

    hash_value = canonical_hash(title, city, starts_at_raw)
    slug = build_unique_slug(title, external_id=external_id)

    categoria = get_or_create_music_category()

    note_raw = {
        "source": "vivaticket",
        "external_id": external_id,
        "subtitle": event_data.get("subtitle"),
        "raw_date": event_data.get("raw_date"),
        "starts_at_raw": starts_at_raw,
        "city": city,
        "venue": event_data.get("venue"),
        "province": event_data.get("province"),
        "organizer": event_data.get("organizer"),
        "shop_type": event_data.get("shop_type"),
        "sale_status": event_data.get("sale_status"),
    }

    piattaforma = Piattaforma.objects.filter(nome="vivaticket").first()

    if piattaforma:
        existing_mapping = (
            EventoPiattaforma.objects
            .filter(
                piattaforma=piattaforma,
                id_evento_piattaforma=str(external_id),
            )
            .select_related("evento")
            .first()
        )

        if existing_mapping:
            evento = existing_mapping.evento
            created = False
            changed = False

            if evento.nome_evento != title:
                evento.nome_evento = title
                evento.nome_evento_normalizzato = normalize_text(title)
                changed = True

            if evento.categoria_id is None:
                evento.categoria = categoria
                changed = True

            if evento.note_raw != note_raw:
                evento.note_raw = note_raw
                changed = True

            if changed:
                evento.save()

            return evento, created

    evento, created = Evento.objects.get_or_create(
        hash_canonico=hash_value,
        defaults={
            "slug": slug,
            "nome_evento": title,
            "nome_evento_normalizzato": normalize_text(title),
            "descrizione": event_data.get("subtitle") or "",
            "stato": "pianificato",
            "genere": "musica",
            "lingua": "it",
            "categoria": categoria,
            "note_raw": note_raw,
        },
    )

    changed = False

    if evento.nome_evento != title:
        evento.nome_evento = title
        evento.nome_evento_normalizzato = normalize_text(title)
        changed = True

    if not evento.slug:
        evento.slug = slug
        changed = True

    if evento.categoria_id is None:
        evento.categoria = categoria
        changed = True

    if evento.note_raw != note_raw:
        evento.note_raw = note_raw
        changed = True

    if changed:
        evento.save()

    return evento, created


def get_or_create_performance(evento: Evento, luogo: Luoghi, event_data: dict):
    """
    Crea o aggiorna Performance.
    """

    starts_at_utc = parse_vivaticket_datetime(event_data.get("starts_at_raw"))

    if not starts_at_utc:
        return None, False

    status = map_performance_status(event_data.get("sale_status"))
    disponibilita_agg = map_availability(event_data.get("sale_status"))

    performance, created = Performance.objects.get_or_create(
        evento=evento,
        luogo=luogo,
        starts_at_utc=starts_at_utc,
        defaults={
            "status": status,
            "disponibilita_agg": disponibilita_agg,
            "valuta": event_data.get("currency") or "EUR",
        },
    )

    changed = False

    if performance.status != status:
        performance.status = status
        changed = True

    if performance.disponibilita_agg != disponibilita_agg:
        performance.disponibilita_agg = disponibilita_agg
        changed = True

    currency = event_data.get("currency") or "EUR"

    if performance.valuta != currency:
        performance.valuta = currency
        changed = True

    if changed:
        performance.save()

    return performance, created


def build_snapshot(event_data: dict) -> dict:
    """
    Snapshot raw salvato sui mapping piattaforma.
    """

    return {
        "source": "vivaticket",
        "title": event_data.get("title"),
        "subtitle": event_data.get("subtitle"),
        "raw_date": event_data.get("raw_date"),
        "starts_at_raw": event_data.get("starts_at_raw"),
        "city": event_data.get("city"),
        "venue": event_data.get("venue"),
        "province": event_data.get("province"),
        "address": event_data.get("address"),
        "organizer": event_data.get("organizer"),
        "performance_id": event_data.get("performance_id"),
        "performance_code": event_data.get("performance_code"),
        "pcode": event_data.get("pcode"),
        "tcode": event_data.get("tcode"),
        "performance_status": event_data.get("performance_status"),
        "is_sell_active": event_data.get("is_sell_active"),
        "sale_status": event_data.get("sale_status"),
        "shop_type": event_data.get("shop_type"),
        "shop_url": event_data.get("shop_url"),
        "source_url": event_data.get("source_url"),
    }


def import_vivaticket_event(event_data: dict, external_id: str, dry_run: bool = True) -> dict:
    """
    Importa un singolo evento Vivaticket nel DB.
    Se dry_run=True non salva nulla.
    """

    if not event_data:
        return {
            "ok": False,
            "reason": "missing_event_data",
        }

    if not external_id:
        return {
            "ok": False,
            "reason": "missing_external_id",
        }

    starts_at_utc = parse_vivaticket_datetime(event_data.get("starts_at_raw"))

    if not starts_at_utc:
        return {
            "ok": False,
            "reason": "missing_or_invalid_datetime",
            "title": event_data.get("title"),
        }

    snapshot = build_snapshot(event_data)

    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "title": event_data.get("title"),
            "city": event_data.get("city"),
            "venue": event_data.get("venue"),
            "starts_at_utc": str(starts_at_utc),
            "external_id": external_id,
            "performance_id": event_data.get("performance_id"),
            "performance_code": event_data.get("performance_code"),
            "sale_status": event_data.get("sale_status"),
            "shop_type": event_data.get("shop_type"),
            "shop_url": event_data.get("shop_url"),
        }

    now = timezone.now()

    piattaforma = get_or_create_vivaticket_platform()
    luogo, luogo_created = get_or_create_place(event_data)
    evento, evento_created = get_or_create_event(event_data, external_id=external_id)
    performance, performance_created = get_or_create_performance(
        evento=evento,
        luogo=luogo,
        event_data=event_data,
    )

    if not performance:
        return {
            "ok": False,
            "reason": "performance_not_created",
            "title": event_data.get("title"),
        }

    evento_mapping, evento_mapping_created = EventoPiattaforma.objects.update_or_create(
        piattaforma=piattaforma,
        id_evento_piattaforma=str(external_id),
        defaults={
            "evento": evento,
            "url": event_data.get("source_url") or "",
            "ultima_scansione": now,
            "snapshot_raw": snapshot,
        },
    )

    raw_perf_id = event_data.get("performance_id")
    shop_type = event_data.get("shop_type")

    if shop_type == "vivaticket_shop" and raw_perf_id:
        external_perf_id = str(raw_perf_id)
    elif raw_perf_id:
        external_perf_id = f"{external_id}:{raw_perf_id}"
    else:
        starts_at_raw = event_data.get("starts_at_raw") or "nodate"
        external_perf_id = f"{external_id}:{starts_at_raw[:10]}"

    performance_mapping, performance_mapping_created = PerformancePiattaforma.objects.update_or_create(
        piattaforma=piattaforma,
        external_perf_id=external_perf_id,
        defaults={
            "performance": performance,
            "url": event_data.get("shop_url") or event_data.get("source_url") or "",
            "ultima_scansione": now,
            "snapshot_raw": snapshot,
        },
    )

    return {
        "ok": True,
        "dry_run": False,
        "evento_id": evento.id,
        "performance_id_db": performance.id,
        "luogo_id": luogo.id,
        "evento_mapping_id": evento_mapping.id,
        "performance_mapping_id": performance_mapping.id,
        "created": {
            "luogo": luogo_created,
            "evento": evento_created,
            "performance": performance_created,
            "evento_mapping": evento_mapping_created,
            "performance_mapping": performance_mapping_created,
        },
        "title": event_data.get("title"),
        "sale_status": event_data.get("sale_status"),
        "shop_type": event_data.get("shop_type"),
    }