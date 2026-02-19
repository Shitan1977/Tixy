# api/management/commands/scrub_ticketmaster_new.py
"""
Ticketmaster NEW (non tocca il vecchio).

Obiettivo: import completo Ticketmaster IT + tutte le date anche senza biglietti.

Punti chiave (robusti, senza “piccioni in volo”):
- Windowing lato scraper (iter_all_events_windowed) per evitare deep paging.
- Import in streaming: NON carichiamo tutto in RAM.
- EventoPiattaforma: checksum stabile; se invariato aggiorna solo ultima_scansione.
- Performance: MAI saltata se abbiamo almeno localDate.
  - Se dates.start.dateTime esiste -> starts_at_utc = quello (UTC)
  - Se manca dateTime ma localDate esiste -> starts_at_utc = localDate a mezzanotte UTC (placeholder tecnico)
    e MARCATORE in disponibilita_agg = "time_tbd" (non usiamo status="TIME_TBD" perché potrebbe non essere ammesso dalle choices).
- Logging finale garantito (try/finally) + progress ogni N record.

USO:
python manage.py scrub_ticketmaster_new --months-ahead 18 --step-days 14 --size 195 --limit 0
(opzionale) --dry-run
(opzionale) --country IT
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone as dt_tz
from typing import Optional, Tuple

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify

from api.models import Piattaforma, Luoghi, Evento, Performance, EventoPiattaforma


def sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def parse_dt_utc(dt_str: Optional[str]) -> Optional[datetime]:
    if not dt_str:
        return None
    # "2026-06-23T19:00:00Z" -> aware UTC
    return datetime.fromisoformat(dt_str.replace("Z", "+00:00")).astimezone(dt_tz.utc)


def parse_local_date_time(e: dict) -> Tuple[Optional[str], Optional[str]]:
    start = (e.get("dates", {}) or {}).get("start", {}) or {}
    return start.get("localDate"), start.get("localTime")


class Command(BaseCommand):
    help = "Scrub Ticketmaster NEW: import completo IT con windowing (streaming, robusto)."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--limit", type=int, default=0)  # 0 = no limit

        parser.add_argument("--months-ahead", type=int, default=18)
        parser.add_argument("--step-days", type=int, default=14)  # consigliato 14
        parser.add_argument("--size", type=int, default=195)

        parser.add_argument("--country", type=str, default="IT")
        parser.add_argument("--source", type=str, default=None)  # opzionale TM source
        parser.add_argument("--include-tba", action="store_true", default=True)
        parser.add_argument("--include-tbd", action="store_true", default=True)

        parser.add_argument("--progress-every", type=int, default=50)

    def unique_slug(self, base_slug: str, *, exclude_pk=None) -> str:
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
        dry_run = bool(options["dry_run"])
        limit = int(options["limit"] or 0)

        months_ahead = int(options["months_ahead"])
        step_days = int(options["step_days"])
        size = int(options["size"])

        country = (options["country"] or "IT").upper().strip()
        source = options["source"] or None
        include_tba = bool(options["include_tba"])
        include_tbd = bool(options["include_tbd"])

        progress_every = max(1, int(options["progress_every"] or 50))

        self.stdout.write(self.style.SUCCESS("=== SCRUB TICKETMASTER NEW START ==="))
        self.stdout.write(f"Time: {timezone.now().isoformat()}")
        self.stdout.write(f"Dry-run: {dry_run}")
        self.stdout.write(
            f"Country: {country} | months_ahead={months_ahead} step_days={step_days} size={size} limit={limit or '∞'}"
        )

        from api.scrapers.ticketmaster_new import iter_all_events_windowed, stable_checksum

        now = timezone.now()

        plat, _ = Piattaforma.objects.get_or_create(
            nome="ticketmaster",
            defaults={"dominio": "ticketmaster.it", "attivo": True},
        )

        created_evt = created_perf = created_map = 0
        updated_evt = updated_perf = updated_map = 0
        skipped_unchanged_map = 0

        perf_placeholder_midnight = 0
        perf_time_tbd_marked = 0

        processed = 0
        failed = 0

        # iterator streaming
        events_iter = iter_all_events_windowed(
            country_code=country,
            months_ahead=months_ahead,
            step_days=step_days,
            size=size,
            include_tba=include_tba,
            include_tbd=include_tbd,
            source=source,
        )

        try:
            for i, e in enumerate(events_iter, start=1):
                if limit and i > limit:
                    break

                tm_id = e.get("id")
                if not tm_id:
                    continue

                name = e.get("name") or ""
                url = e.get("url") or ""

                # dateTime (UTC) se esiste
                dates_start = (e.get("dates", {}) or {}).get("start", {}) or {}
                dt_str = dates_start.get("dateTime")  # ISO Z
                starts_at_utc = parse_dt_utc(dt_str)

                # localDate/localTime (sempre utili)
                local_date, local_time = parse_local_date_time(e)

                # venue
                venues = (e.get("_embedded", {}) or {}).get("venues", []) or []
                v0 = venues[0] if venues else {}
                venue_name = v0.get("name") or "Sconosciuto"
                city = (v0.get("city") or {}).get("name") or ""
                country_code = (v0.get("country") or {}).get("countryCode") or country

                luogo_norm = slugify(f"{venue_name}-{city}-{country_code}") or slugify(venue_name) or "luogo"
                evento_norm = slugify(name) or "evento"

                hash_canonico = sha256(f"ticketmaster:{tm_id}")
                slug_base = f"{evento_norm}-{(tm_id or '')[:8]}".strip("-")

                checksum = stable_checksum(e)

                if dry_run:
                    self.stdout.write(
                        f"[DRY] {tm_id} | {name} | starts_at_utc={starts_at_utc} | "
                        f"local={local_date} {local_time} | {venue_name} ({city},{country_code})"
                    )
                    processed += 1
                    if processed % progress_every == 0:
                        self.stdout.write(f"...progress(dry): {processed}")
                    continue

                # import DB (atomico per record)
                try:
                    with transaction.atomic():
                        # --- LUOGO ---
                        luogo, _ = Luoghi.objects.get_or_create(
                            nome_normalizzato=luogo_norm,
                            defaults={
                                "nome": venue_name,
                                "indirizzo": v0.get("address", {}).get("line1"),
                                "citta": city or None,
                                "citta_normalizzata": slugify(city) if city else None,
                                "stato_iso": country_code or None,
                                "timezone": v0.get("timezone"),
                            },
                        )

                        changed_luogo = False
                        if venue_name and luogo.nome != venue_name:
                            luogo.nome = venue_name
                            changed_luogo = True
                        if city and luogo.citta != city:
                            luogo.citta = city
                            changed_luogo = True
                        if country_code and luogo.stato_iso != country_code:
                            luogo.stato_iso = country_code
                            changed_luogo = True
                        if changed_luogo:
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
                            changed_evt = False
                            if name and evento.nome_evento != name:
                                evento.nome_evento = name
                                evento.nome_evento_normalizzato = evento_norm
                                changed_evt = True

                            desired = self.unique_slug(slug_base, exclude_pk=evento.pk)
                            if desired and evento.slug != desired:
                                evento.slug = desired
                                changed_evt = True

                            if changed_evt:
                                evento.save(update_fields=["nome_evento", "nome_evento_normalizzato", "slug", "aggiornato_il"])
                                updated_evt += 1

                        # --- MAPPING EventoPiattaforma ---
                        mapping = (
                            EventoPiattaforma.objects.filter(piattaforma=plat, id_evento_piattaforma=tm_id)
                            .select_for_update()
                            .first()
                        )

                        if mapping is None:
                            EventoPiattaforma.objects.create(
                                piattaforma=plat,
                                id_evento_piattaforma=tm_id,
                                evento=evento,
                                url=url,
                                ultima_scansione=now,
                                snapshot_raw=e,
                                checksum_dati=checksum,
                            )
                            created_map += 1
                        else:
                            if (mapping.checksum_dati or "") == checksum:
                                mapping.ultima_scansione = now
                                mapping.save(update_fields=["ultima_scansione", "aggiornato_il"])
                                skipped_unchanged_map += 1
                            else:
                                mapping.evento = evento
                                if url:
                                    mapping.url = url
                                mapping.ultima_scansione = now
                                mapping.snapshot_raw = e
                                mapping.checksum_dati = checksum
                                mapping.save(update_fields=[
                                    "evento", "url", "ultima_scansione", "snapshot_raw", "checksum_dati", "aggiornato_il"
                                ])
                                updated_map += 1

                        # --- PERFORMANCE (robusta) ---
                        # Mai saltare se abbiamo localDate.
                        perf_starts = starts_at_utc
                        perf_status = "UNKNOWN"
                        perf_dispo = None

                        if perf_starts is None:
                            if local_date:
                                perf_starts = datetime.fromisoformat(local_date).replace(
                                    hour=0, minute=0, second=0, microsecond=0, tzinfo=dt_tz.utc
                                )
                                perf_dispo = "time_tbd"
                                perf_placeholder_midnight += 1
                            else:
                                # niente dateTime e niente localDate -> non possiamo creare performance
                                # (ma evento+mapping già salvati)
                                processed += 1
                                continue

                        perf, perf_created = Performance.objects.get_or_create(
                            evento=evento,
                            luogo=luogo,
                            starts_at_utc=perf_starts,
                            defaults={
                                "status": perf_status,
                                "disponibilita_agg": perf_dispo or "sconosciuta",
                                "valuta": "EUR",
                            },
                        )

                        if perf_created:
                            created_perf += 1
                            if perf_dispo == "time_tbd":
                                if perf.disponibilita_agg != "time_tbd":
                                    perf.disponibilita_agg = "time_tbd"
                                    changed_perf = True
                                if perf.status != "TIME_TBD":
                                    perf.status = "TIME_TBD"
                                    changed_perf = True

                                perf_time_tbd_marked += 1
                        else:
                            changed_perf = False
                            if perf_dispo == "time_tbd" and perf.disponibilita_agg != "time_tbd":
                                perf.disponibilita_agg = "time_tbd"
                                changed_perf = True

                            if changed_perf:
                                perf.save(update_fields=["disponibilita_agg", "aggiornato_il"])
                                updated_perf += 1

                    processed += 1

                except Exception as ex:
                    failed += 1
                    self.stderr.write(self.style.WARNING(f"[ERROR] tm_id={tm_id} name={name!r} err={ex}"))
                    # continuiamo (non blocchiamo il job)

                if processed and processed % progress_every == 0:
                    self.stdout.write(
                        f"...progress: processed={processed} failed={failed} created_evt={created_evt} created_perf={created_perf}"
                    )

        finally:
            # riepilogo sempre stampato
            self.stdout.write(self.style.SUCCESS(
                "DB OK - "
                f"processed={processed} failed={failed} | "
                f"created eventi={created_evt}, performances={created_perf}, mappings={created_map} | "
                f"updated eventi={updated_evt}, performances={updated_perf}, mappings={updated_map} | "
                f"unchanged mappings={skipped_unchanged_map} | "
                f"perf placeholder-midnight={perf_placeholder_midnight} time_tbd_marked={perf_time_tbd_marked}"
            ))
            self.stdout.write(self.style.SUCCESS("=== SCRUB TICKETMASTER NEW END ==="))