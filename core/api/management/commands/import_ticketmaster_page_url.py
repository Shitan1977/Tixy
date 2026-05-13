import hashlib
import json
import re
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime
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


def normalize_text(value: str) -> str:
    """
    Normalizza una stringa per confronti, slug e chiavi tecniche.
    Esempio:
    'Stadio San Siro' -> 'stadio san siro'
    """
    value = value or ""
    value = value.strip().lower()
    value = re.sub(r"\s+", " ", value)
    return value


def sha256_text(value: str) -> str:
    """
    Crea un hash stabile da una stringa.
    Lo usiamo per hash_canonico e checksum_dati.
    """
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def clean_ticketmaster_url(url: str) -> str:
    """
    Rimuove query string temporanee, per esempio queueittoken.
    Manteniamo solo schema, dominio e path.
    """
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def extract_event_id_from_url(url: str) -> Optional[str]:
    """
    Estrae l'id evento dal formato:
    /event/3yp92pfdbd70/ticketmaster
    """
    match = re.search(r"/event/([^/?#]+)", url)
    if not match:
        return None
    return match.group(1)


def fetch_html(url: str) -> str:
    """
    Scarica la pagina Ticketmaster con headers browser-like.
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120 Safari/537.36"
        ),
        "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
    }

    response = requests.get(url, headers=headers, timeout=30)
    response.raise_for_status()
    return response.text


def extract_event_json_ld(html: str) -> dict:
    """
    Cerca nello HTML il blocco JSON-LD di tipo Event.

    Ticketmaster inserisce spesso i dati evento in:
    <script type="application/ld+json">...</script>
    """
    soup = BeautifulSoup(html, "html.parser")
    scripts = soup.find_all("script", {"type": "application/ld+json"})

    for script in scripts:
        raw = script.string or script.get_text()
        if not raw:
            continue

        try:
            data = json.loads(raw)
        except Exception:
            continue

        if isinstance(data, dict) and data.get("@type") == "Event":
            return data

    raise CommandError("Nessun JSON-LD di tipo Event trovato nella pagina.")


def parse_start_date(start_date: str):
    """
    Converte startDate JSON-LD in datetime timezone aware.
    Esempio:
    2027-07-24T18:00:00.000Z
    """
    if not start_date:
        raise CommandError("startDate mancante nel JSON-LD.")

    dt = parse_datetime(start_date)

    if not dt:
        raise CommandError(f"startDate non valido: {start_date}")

    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, timezone=timezone.utc)

    return dt


def build_unique_slug(base: str) -> str:
    """
    Crea uno slug unico per Evento.
    Se esiste già, aggiunge -2, -3, ecc.
    """
    base_slug = slugify(base)[:220] or "evento-ticketmaster"
    candidate = base_slug
    counter = 2

    while Evento.objects.filter(slug=candidate).exists():
        candidate = f"{base_slug}-{counter}"
        counter += 1

    return candidate


class Command(BaseCommand):
    help = "Importa una pagina evento Ticketmaster leggibile tramite JSON-LD."

    def add_arguments(self, parser):
        parser.add_argument("--url", required=True, help="URL pagina evento Ticketmaster")
        parser.add_argument("--dry-run", action="store_true", help="Mostra cosa farebbe senza salvare")

    def handle(self, *args, **options):
        input_url = options["url"]
        dry_run = options["dry_run"]

        clean_url = clean_ticketmaster_url(input_url)
        external_id = extract_event_id_from_url(clean_url)

        if not external_id:
            raise CommandError("Impossibile estrarre external event id dall'URL.")

        self.stdout.write("=== IMPORT TICKETMASTER PAGE URL START ===")
        self.stdout.write(f"URL: {clean_url}")
        self.stdout.write(f"External ID: {external_id}")
        self.stdout.write(f"Dry-run: {dry_run}")

        html = fetch_html(clean_url)
        event_data = extract_event_json_ld(html)

        name = event_data.get("name") or ""
        description = event_data.get("description") or ""
        image_url = event_data.get("image") or ""
        start_date_raw = event_data.get("startDate") or ""

        location = event_data.get("location") or {}
        address = location.get("address") or {}

        venue_name = location.get("name") or ""
        city = address.get("addressLocality") or ""
        country = address.get("addressCountry") or ""

        geo = location.get("geo") or {}
        lat = geo.get("latitude")
        lng = geo.get("longitude")

        offers = event_data.get("offers") or {}
        currency = offers.get("priceCurrency") or "EUR"

        starts_at_utc = parse_start_date(start_date_raw)

        if not name:
            raise CommandError("Nome evento mancante nel JSON-LD.")

        if not venue_name:
            raise CommandError("Luogo evento mancante nel JSON-LD.")

        if not city:
            raise CommandError("Città evento mancante nel JSON-LD.")

        normalized_name = normalize_text(name)
        normalized_city = normalize_text(city)
        normalized_venue = normalize_text(venue_name)

        event_hash = sha256_text(f"ticketmaster_page:{external_id}")
        checksum = sha256_text(json.dumps(event_data, sort_keys=True, ensure_ascii=False))

        self.stdout.write("")
        self.stdout.write("--- DATI ESTRATTI ---")
        self.stdout.write(f"Titolo: {name}")
        self.stdout.write(f"Descrizione: {description}")
        self.stdout.write(f"Data UTC: {starts_at_utc}")
        self.stdout.write(f"Venue: {venue_name}")
        self.stdout.write(f"Città: {city}")
        self.stdout.write(f"Country: {country}")
        self.stdout.write(f"Valuta: {currency}")
        self.stdout.write(f"Immagine: {image_url}")

        if dry_run:
            self.stdout.write(self.style.WARNING(""))
            self.stdout.write(self.style.WARNING("DRY-RUN attivo: nessun dato salvato."))
            self.stdout.write(self.style.SUCCESS("=== IMPORT TICKETMASTER PAGE URL END ==="))
            return

        with transaction.atomic():
            piattaforma, _ = Piattaforma.objects.get_or_create(
                nome="ticketmaster",
                defaults={
                    "dominio": "ticketmaster.it",
                    "attivo": True,
                },
            )

            categoria, _ = Categoria.objects.get_or_create(
                slug="musica",
                defaults={
                    "nome": "Musica",
                },
            )

            luogo, luogo_created = Luoghi.objects.get_or_create(
                nome_normalizzato=normalized_venue,
                defaults={
                    "nome": venue_name,
                    "indirizzo": address.get("streetAddress") or None,
                    "citta": city,
                    "citta_normalizzata": normalized_city,
                    "stato_iso": "IT" if country.lower() in ["italy", "italia", "it"] else None,
                    "lat": lat,
                    "lng": lng,
                    "timezone": "Europe/Rome",
                },
            )

            # Se il luogo esisteva già, aggiorniamo solo campi utili mancanti.
            luogo_updated = False

            if not luogo.citta and city:
                luogo.citta = city
                luogo_updated = True

            if not luogo.citta_normalizzata and normalized_city:
                luogo.citta_normalizzata = normalized_city
                luogo_updated = True

            if not luogo.stato_iso and country:
                luogo.stato_iso = "IT" if country.lower() in ["italy", "italia", "it"] else None
                luogo_updated = True

            if not luogo.lat and lat:
                luogo.lat = lat
                luogo_updated = True

            if not luogo.lng and lng:
                luogo.lng = lng
                luogo_updated = True

            if luogo_updated:
                luogo.save()

            evento = Evento.objects.filter(hash_canonico=event_hash).first()

            evento_created = False
            if not evento:
                evento = Evento.objects.create(
                    slug=build_unique_slug(f"{name}-{external_id}"),
                    nome_evento=name,
                    nome_evento_normalizzato=normalized_name,
                    descrizione=description,
                    stato="pianificato",
                    categoria=categoria,
                    hash_canonico=event_hash,
                    immagine_url=image_url,
                    note_raw={
                        "source": "ticketmaster_page_url",
                        "external_id": external_id,
                        "url": clean_url,
                        "json_ld": event_data,
                    },
                )
                evento_created = True
            else:
                evento.nome_evento = name
                evento.nome_evento_normalizzato = normalized_name
                evento.descrizione = description
                evento.immagine_url = image_url
                evento.categoria = categoria
                evento.note_raw = {
                    "source": "ticketmaster_page_url",
                    "external_id": external_id,
                    "url": clean_url,
                    "json_ld": event_data,
                }
                evento.save()

            performance, performance_created = Performance.objects.get_or_create(
                evento=evento,
                luogo=luogo,
                starts_at_utc=starts_at_utc,
                defaults={
                    "status": "ONSALE",
                    "disponibilita_agg": "sconosciuta",
                    "valuta": currency,
                },
            )

            performance.valuta = currency
            performance.save()

            evento_mapping, evento_mapping_created = EventoPiattaforma.objects.update_or_create(
                piattaforma=piattaforma,
                id_evento_piattaforma=external_id,
                defaults={
                    "evento": evento,
                    "url": clean_url,
                    "ultima_scansione": timezone.now(),
                    "snapshot_raw": event_data,
                    "checksum_dati": checksum,
                },
            )

            perf_mapping, perf_mapping_created = PerformancePiattaforma.objects.update_or_create(
                piattaforma=piattaforma,
                external_perf_id=external_id,
                defaults={
                    "performance": performance,
                    "url": clean_url,
                    "ultima_scansione": timezone.now(),
                    "snapshot_raw": event_data,
                    "checksum_dati": checksum,
                },
            )

        self.stdout.write("")
        self.stdout.write("--- RISULTATO DB ---")
        self.stdout.write(f"Luogo creato: {luogo_created}")
        self.stdout.write(f"Evento creato: {evento_created}")
        self.stdout.write(f"Performance creata: {performance_created}")
        self.stdout.write(f"EventoPiattaforma creato: {evento_mapping_created}")
        self.stdout.write(f"PerformancePiattaforma creato: {perf_mapping_created}")

        self.stdout.write("")
        self.stdout.write(f"Evento ID: {evento.id}")
        self.stdout.write(f"Performance ID: {performance.id}")
        self.stdout.write(self.style.SUCCESS("=== IMPORT TICKETMASTER PAGE URL END ==="))