from django.core.management.base import BaseCommand
from django.utils import timezone
from django.db import transaction
from django.utils.text import slugify
import hashlib
from datetime import datetime

from api.models import Piattaforma, Luoghi, Evento, Performance, EventoPiattaforma


def sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def parse_dt_utc(dt_str: str):
    if not dt_str:
        return None
    return datetime.fromisoformat(dt_str.replace("Z", "+00:00"))


class Command(BaseCommand):
    help = "Scrub portali: prima versione (Ticketmaster)."

    def add_arguments(self, parser):
        parser.add_argument("--source", type=str, default="ticketmaster")
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--limit", type=int, default=10)

    def unique_slug(self, base_slug: str, *, exclude_pk=None) -> str:
        """
        Genera uno slug unico rispettando il vincolo UNIQUE su Evento.slug.
        Se base_slug è già occupato, prova base_slug-2, base_slug-3, ...
        """
        base_slug = (base_slug or "").strip("-") or "evento"
        slug = base_slug
        i = 2

        qs = Evento.objects.all()
        if exclude_pk:
            qs = qs.exclude(pk=exclude_pk)

        while qs.filter(slug=slug).exists():
            slug = f"{base_slug}-{i}"
            i += 1

        return slug

    def handle(self, *args, **options):
        source = (options["source"] or "").lower().strip()
        dry_run = bool(options["dry_run"])
        limit = int(options["limit"])

        self.stdout.write(self.style.SUCCESS("=== SCRUB PORTALS START ==="))
        self.stdout.write(f"Time: {timezone.now().isoformat()}")
        self.stdout.write(f"Source: {source}")
        self.stdout.write(f"Dry-run: {dry_run}")

        if source == "ticketmaster":
            from api.scrapers.ticketmaster import iter_all_events

            # Prendiamo fino a "limit" eventi, iterando tutte le pagine (iter_all_events già lo fa).
            events = []
            for i, e in enumerate(iter_all_events(country_code="IT", size=200), start=1):
                events.append(e)
                if limit and i >= limit:
                    break

            self.stdout.write(self.style.SUCCESS(f"Ticketmaster events fetched: {len(events)}"))

            now = timezone.now()

            plat, _ = Piattaforma.objects.get_or_create(
                nome="ticketmaster",
                defaults={"dominio": "ticketmaster.it", "attivo": True},
            )

            created_evt = created_perf = created_map = 0
            skipped_perf = 0

            for e in events:
                tm_id = e.get("id")
                name = e.get("name") or ""
                url = e.get("url") or ""

                dates = e.get("dates", {}).get("start", {}) or {}
                dt_str = dates.get("dateTime")
                starts_at = parse_dt_utc(dt_str)

                venues = (e.get("_embedded", {}) or {}).get("venues", []) or []
                v0 = venues[0] if venues else {}
                venue_name = v0.get("name") or "Sconosciuto"
                city = (v0.get("city") or {}).get("name") or ""
                country = (v0.get("country") or {}).get("countryCode") or ""

                luogo_norm = slugify(f"{venue_name}-{city}-{country}") or slugify(venue_name) or "luogo"
                evento_norm = slugify(name) or "evento"
                hash_canonico = sha256(f"ticketmaster:{tm_id}")

                # slug deterministico (ma reso sempre unico in DB)
                slug_base = f"{evento_norm}-{(tm_id or '')[:8]}".strip("-")

                if dry_run:
                    self.stdout.write(
                        f"[DRY] EVENTO: {name} | {starts_at} | {venue_name} ({city},{country}) | tm_id={tm_id}"
                    )
                    continue

                with transaction.atomic():
                    # --- LUOGO ---
                    luogo, _ = Luoghi.objects.get_or_create(
                        nome_normalizzato=luogo_norm,
                        defaults={
                            "nome": venue_name,
                            "indirizzo": v0.get("address", {}).get("line1"),
                            "citta": city or None,
                            "citta_normalizzata": slugify(city) if city else None,
                            "stato_iso": country or None,
                            "timezone": v0.get("timezone"),
                        },
                    )
                    luogo.nome = venue_name
                    luogo.citta = city or luogo.citta
                    luogo.stato_iso = country or luogo.stato_iso
                    luogo.save(update_fields=["nome", "citta", "stato_iso", "aggiornato_il"])

                    # --- EVENTO ---
                    safe_slug_on_create = self.unique_slug(slug_base)

                    evento, evento_created = Evento.objects.get_or_create(
                        hash_canonico=hash_canonico,
                        defaults={
                            "slug": safe_slug_on_create,
                            "nome_evento": name,
                            "nome_evento_normalizzato": evento_norm,
                            "stato": "pianificato",
                            "note_raw": {"source": "ticketmaster"},
                        },
                    )

                    if evento_created:
                        created_evt += 1
                    else:
                        changed = False

                        if evento.nome_evento != name and name:
                            evento.nome_evento = name
                            evento.nome_evento_normalizzato = evento_norm
                            changed = True

                        # Riallineo slug in modo SAFE (mai collisioni)
                        desired = self.unique_slug(slug_base, exclude_pk=evento.pk)
                        if desired and evento.slug != desired:
                            evento.slug = desired
                            changed = True

                        if changed:
                            evento.save(update_fields=["nome_evento", "nome_evento_normalizzato", "slug", "aggiornato_il"])

                    # --- MAPPING (sempre, anche senza data) ---
                    mapping, map_created = EventoPiattaforma.objects.get_or_create(
                        piattaforma=plat,
                        id_evento_piattaforma=tm_id,
                        defaults={
                            "evento": evento,
                            "url": url,
                            "ultima_scansione": now,
                            "snapshot_raw": e,
                            "checksum_dati": sha256(str(e)),
                        },
                    )
                    if map_created:
                        created_map += 1
                    else:
                        mapping.evento = evento
                        mapping.url = url or mapping.url
                        mapping.ultima_scansione = now
                        mapping.snapshot_raw = e
                        mapping.checksum_dati = sha256(str(e))
                        mapping.save(update_fields=[
                            "evento", "url", "ultima_scansione", "snapshot_raw", "checksum_dati", "aggiornato_il"
                        ])

                    # --- PERFORMANCE (solo se c'è dateTime) ---
                    if starts_at is None:
                        skipped_perf += 1
                        self.stdout.write(self.style.WARNING(
                            f"[SKIP PERF] Missing dateTime (TBA/TBD?) | tm_id={tm_id} | {name}"
                        ))
                        continue

                    perf, perf_created = Performance.objects.get_or_create(
                        evento=evento,
                        luogo=luogo,
                        starts_at_utc=starts_at,
                        defaults={"status": "ONSALE", "disponibilita_agg": "sconosciuta", "valuta": "EUR"},
                    )
                    if perf_created:
                        created_perf += 1

            self.stdout.write(self.style.SUCCESS(
                f"DB OK - created eventi={created_evt}, performances={created_perf}, "
                f"mappings={created_map}, skipped_perf={skipped_perf}"
            ))

        else:
            self.stdout.write(self.style.WARNING("Source non gestita ancora."))

        self.stdout.write(self.style.SUCCESS("=== SCRUB PORTALS END ==="))

