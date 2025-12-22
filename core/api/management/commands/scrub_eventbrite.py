from django.core.management.base import BaseCommand
from django.utils import timezone
from django.db import transaction
from django.utils.text import slugify

import os
import hashlib
from datetime import datetime

from api.models import Piattaforma, Luoghi, Evento, Performance, EventoPiattaforma
from api.scrapers.eventbrite import EventbriteClient


def sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def parse_dt_utc(dt_str: str):
    if not dt_str:
        return None
    return datetime.fromisoformat(dt_str.replace("Z", "+00:00"))


class Command(BaseCommand):
    help = "Scrub Eventbrite: eventi + venue + mapping (dedup) per una organization."

    def add_arguments(self, parser):
        parser.add_argument("--org-id", required=True, type=str, help="Eventbrite organization_id")
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--limit", type=int, default=2000)
        parser.add_argument("--page-size", type=int, default=50)

    def handle(self, *args, **options):
        org_id = str(options["org_id"]).strip()
        dry_run = bool(options["dry_run"])
        target_total = int(options["limit"])
        page_size = int(options["page_size"])

        token = (os.getenv("EVENTBRITE_TOKEN") or "").strip()
        if not token:
            self.stdout.write(self.style.ERROR("Manca EVENTBRITE_TOKEN in env."))
            return

        self.stdout.write(self.style.SUCCESS("=== SCRUB EVENTBRITE START ==="))
        self.stdout.write(f"Time: {timezone.now().isoformat()}")
        self.stdout.write(f"Org ID: {org_id}")
        self.stdout.write(f"Dry-run: {dry_run}")
        self.stdout.write(f"Target events: {target_total} | Page size: {page_size}")

        now = timezone.now()

        # piattaforma (idempotente)
        plat, _ = Piattaforma.objects.get_or_create(
            nome="eventbrite",
            defaults={"dominio": "eventbrite.com", "attivo": True},
        )

        # 1) FETCH (paginazione gestita nello scraper)
        client = EventbriteClient(token)
        events = client.fetch_org_events(org_id=org_id, status="all", page_size=page_size)
        events = events[:target_total]

        self.stdout.write(self.style.SUCCESS(f"Eventbrite events fetched: {len(events)}"))

        created_evt = created_perf = created_map = 0
        skipped_same_checksum = 0
        updated_existing = 0

        # 2) PROCESS + UPSERT (stesso stile di Ticketmaster)
        for e in events:
            ext_id = e.get("external_event_id")
            name = e.get("title") or ""
            url = e.get("url") or ""

            starts_at = parse_dt_utc(e.get("starts_at_iso"))

            venue_name = e.get("venue_name") or "Sconosciuto"
            city = e.get("city") or ""
            country = e.get("country") or ""

            luogo_norm = slugify(f"{venue_name}-{city}-{country}") or slugify(venue_name) or "luogo"
            evento_norm = slugify(name) or "evento"

            # hash canonico: stabile per Eventbrite+id
            hash_canonico = sha256(f"eventbrite:{ext_id}")

            slug = f"{evento_norm}-{(ext_id or '')[:8]}".strip("-")

            checksum_now = sha256(str(e.get("raw", e)))

            if dry_run:
                self.stdout.write(
                    f"[DRY] EVENTO: {name} | {starts_at} | {venue_name} ({city},{country}) | eb_id={ext_id}"
                )
                continue

            existing = (
                EventoPiattaforma.objects
                .filter(piattaforma=plat, id_evento_piattaforma=ext_id)
                .only("id", "checksum_dati")
                .first()
            )
            if existing and existing.checksum_dati == checksum_now:
                EventoPiattaforma.objects.filter(id=existing.id).update(ultima_scansione=now)
                skipped_same_checksum += 1
                continue

            with transaction.atomic():
                # Luogo
                luogo, _ = Luoghi.objects.get_or_create(
                    nome_normalizzato=luogo_norm,
                    defaults={
                        "nome": venue_name,
                        "indirizzo": None,
                        "citta": city or None,
                        "citta_normalizzata": slugify(city) if city else None,
                        "stato_iso": country or None,
                        "timezone": None,
                    },
                )

                upd_fields = []
                if venue_name and luogo.nome != venue_name:
                    luogo.nome = venue_name
                    upd_fields.append("nome")
                if city and luogo.citta != city:
                    luogo.citta = city
                    upd_fields.append("citta")
                if country and luogo.stato_iso != country:
                    luogo.stato_iso = country
                    upd_fields.append("stato_iso")
                if upd_fields:
                    upd_fields.append("aggiornato_il")
                    luogo.save(update_fields=upd_fields)

                # Evento
                evento, created = Evento.objects.get_or_create(
                    hash_canonico=hash_canonico,
                    defaults={
                        "slug": slug,
                        "nome_evento": name,
                        "nome_evento_normalizzato": evento_norm,
                        "stato": "pianificato",
                        "note_raw": {"source": "eventbrite"},
                    },
                )
                if created:
                    created_evt += 1
                else:
                    changed = False
                    if name and evento.nome_evento != name:
                        evento.nome_evento = name
                        evento.nome_evento_normalizzato = evento_norm
                        changed = True
                    if slug and evento.slug != slug:
                        evento.slug = slug
                        changed = True
                    if changed:
                        evento.save(update_fields=["nome_evento", "nome_evento_normalizzato", "slug", "aggiornato_il"])
                        updated_existing += 1

                # Performance
                if starts_at is not None:
                    perf, perf_created = Performance.objects.get_or_create(
                        evento=evento,
                        luogo=luogo,
                        starts_at_utc=starts_at,
                        defaults={
                            "status": "ONSALE",
                            "disponibilita_agg": "sconosciuta",
                            "valuta": "EUR",
                        },
                    )
                    if perf_created:
                        created_perf += 1

                # Mapping evento-piattaforma
                mapping, map_created = EventoPiattaforma.objects.get_or_create(
                    piattaforma=plat,
                    id_evento_piattaforma=ext_id,
                    defaults={
                        "evento": evento,
                        "url": url,
                        "ultima_scansione": now,
                        "snapshot_raw": e.get("raw", e),
                        "checksum_dati": checksum_now,
                    },
                )
                if map_created:
                    created_map += 1
                else:
                    mapping.evento = evento
                    if url:
                        mapping.url = url
                    mapping.ultima_scansione = now
                    mapping.snapshot_raw = e.get("raw", e)
                    mapping.checksum_dati = checksum_now
                    mapping.save(update_fields=["evento", "url", "ultima_scansione", "snapshot_raw", "checksum_dati", "aggiornato_il"])

        self.stdout.write(self.style.SUCCESS(
            f"DB OK - created eventi={created_evt}, performances={created_perf}, mappings={created_map} | "
            f"skipped_same_checksum={skipped_same_checksum} | updated_existing={updated_existing}"
        ))
        self.stdout.write(self.style.SUCCESS("=== SCRUB EVENTBRITE END ==="))
