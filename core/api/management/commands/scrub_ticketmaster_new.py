# api/management/commands/scrub_ticketmaster_new.py
"""
Ticketmaster NEW.

Obiettivo:
- import completo Ticketmaster IT;
- gestione finestre temporali per evitare deep paging;
- import streaming, senza caricare tutto in RAM;
- creazione/aggiornamento Evento, Luogo, Performance;
- mapping generale su EventoPiattaforma;
- mapping specifico della singola data su PerformancePiattaforma.

Nota importante:
Ticketmaster può restituire più ID/URL per lo stesso evento o per più date
dello stesso evento. Per questo:

- EventoPiattaforma viene usato come mapping generale evento/piattaforma;
- PerformancePiattaforma viene usato come mapping preciso della singola data.

Questo evita errori tipo:
Duplicate entry "...-4" for key "uq_evento_plat_pair"

PATCH:
  D - TicketmasterError catturata nel loop esterno: il comando non crasha
      silenziosamente; l'errore va su stderr e il finally stampa i contatori reali.
  E - Counter finestre: quante processate, quante con 0 eventi, quante con 429.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone as dt_tz
from typing import Optional, Tuple

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify

from api.models import (
    Piattaforma,
    Luoghi,
    Evento,
    Performance,
    EventoPiattaforma,
    PerformancePiattaforma,
)
from api.services.performance_matching import find_best_matching_performance


def sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def parse_dt_utc(dt_str: Optional[str]) -> Optional[datetime]:
    """
    Converte una data ISO Ticketmaster in datetime aware UTC.

    Esempio:
        "2026-06-23T19:00:00Z"
    """
    if not dt_str:
        return None

    return datetime.fromisoformat(dt_str.replace("Z", "+00:00")).astimezone(dt_tz.utc)


def parse_local_date_time(e: dict) -> Tuple[Optional[str], Optional[str]]:
    """
    Estrae localDate e localTime dal payload Ticketmaster.
    """
    start = (e.get("dates", {}) or {}).get("start", {}) or {}

    return start.get("localDate"), start.get("localTime")


def upsert_evento_piattaforma_safe(
    *,
    plat: Piattaforma,
    evento: Evento,
    tm_id: str,
    url: str,
    snapshot_raw: dict,
    checksum: str,
    now,
) -> str:
    """
    Crea/aggiorna il mapping generale EventoPiattaforma.

    Vincoli reali del model:
    - piattaforma + id_evento_piattaforma è unico;
    - evento + piattaforma è unico.

    Quindi prima cerchiamo:
    1. mapping con lo stesso external ID;
    2. mapping con lo stesso evento + piattaforma.

    Se esiste già un mapping per evento + piattaforma, non ne creiamo un altro.
    Questo evita l'errore uq_evento_plat_pair.

    Ritorna:
        "created" | "updated" | "unchanged"
    """

    mapping_by_external = (
        EventoPiattaforma.objects
        .select_for_update()
        .filter(
            piattaforma=plat,
            id_evento_piattaforma=tm_id,
        )
        .first()
    )

    mapping_by_pair = (
        EventoPiattaforma.objects
        .select_for_update()
        .filter(
            piattaforma=plat,
            evento=evento,
        )
        .first()
    )

    if mapping_by_external and mapping_by_pair and mapping_by_external.pk != mapping_by_pair.pk:
        # Caso sporco ma possibile:
        # lo stesso tm_id esiste già su un mapping, ma l'evento ha già un altro mapping generale.
        # Per non violare i vincoli, usiamo il mapping evento/piattaforma come generale
        # e lasciamo il dettaglio preciso a PerformancePiattaforma.
        mapping = mapping_by_pair

    else:
        mapping = mapping_by_external or mapping_by_pair

    if mapping is None:
        EventoPiattaforma.objects.create(
            piattaforma=plat,
            id_evento_piattaforma=tm_id,
            evento=evento,
            url=url,
            ultima_scansione=now,
            snapshot_raw=snapshot_raw,
            checksum_dati=checksum,
        )

        return "created"

    changed = False
    update_fields = ["ultima_scansione", "aggiornato_il"]

    mapping.ultima_scansione = now

    if not mapping.id_evento_piattaforma:
        already_used = (
            EventoPiattaforma.objects
            .filter(
                piattaforma=plat,
                id_evento_piattaforma=tm_id,
            )
            .exclude(pk=mapping.pk)
            .exists()
        )

        if not already_used:
            mapping.id_evento_piattaforma = tm_id
            update_fields.append("id_evento_piattaforma")
            changed = True

    if mapping.evento_id != evento.id:
        pair_exists = (
            EventoPiattaforma.objects
            .filter(
                piattaforma=plat,
                evento=evento,
            )
            .exclude(pk=mapping.pk)
            .exists()
        )

        if not pair_exists:
            mapping.evento = evento
            update_fields.append("evento")
            changed = True

    if url and mapping.url != url:
        mapping.url = url
        update_fields.append("url")
        changed = True

    if (mapping.checksum_dati or "") != checksum:
        mapping.snapshot_raw = snapshot_raw
        mapping.checksum_dati = checksum
        update_fields.extend(["snapshot_raw", "checksum_dati"])
        changed = True

    mapping.save(update_fields=list(dict.fromkeys(update_fields)))

    return "updated" if changed else "unchanged"


def upsert_performance_piattaforma_safe(
    *,
    plat: Piattaforma,
    perf: Performance,
    tm_id: str,
    url: str,
    snapshot_raw: dict,
    checksum: str,
    now,
) -> str:
    """
    Crea/aggiorna il mapping specifico PerformancePiattaforma.

    Questo è il mapping corretto per Ticketmaster quando una singola data
    ha un proprio ID esterno e una propria URL.

    Ritorna:
        "created" | "updated" | "unchanged"
    """

    pp = (
        PerformancePiattaforma.objects
        .select_for_update()
        .filter(
            piattaforma=plat,
            external_perf_id=tm_id,
        )
        .first()
    )

    if pp is None:
        PerformancePiattaforma.objects.create(
            piattaforma=plat,
            performance=perf,
            external_perf_id=tm_id,
            url=url,
            ultima_scansione=now,
            snapshot_raw=snapshot_raw,
            checksum_dati=checksum,
        )

        return "created"

    changed = False
    update_fields = ["ultima_scansione", "aggiornato_il"]

    pp.ultima_scansione = now

    if pp.performance_id != perf.id:
        pp.performance = perf
        update_fields.append("performance")
        changed = True

    if url and pp.url != url:
        pp.url = url
        update_fields.append("url")
        changed = True

    if (pp.checksum_dati or "") != checksum:
        pp.snapshot_raw = snapshot_raw
        pp.checksum_dati = checksum
        update_fields.extend(["snapshot_raw", "checksum_dati"])
        changed = True

    pp.save(update_fields=list(dict.fromkeys(update_fields)))

    return "updated" if changed else "unchanged"


class Command(BaseCommand):
    help = "Scrub Ticketmaster NEW: import completo IT con windowing, streaming e mapping performance."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--limit", type=int, default=0)

        parser.add_argument("--months-ahead", type=int, default=18)
        parser.add_argument("--step-days", type=int, default=14)
        parser.add_argument("--size", type=int, default=195)

        parser.add_argument("--country", type=str, default="IT")
        parser.add_argument("--source", type=str, default=None)
        parser.add_argument("--include-tba", action="store_true", default=True)
        parser.add_argument("--include-tbd", action="store_true", default=True)

        parser.add_argument("--progress-every", type=int, default=50)

        parser.add_argument(
            "--match-dry-run",
            action="store_true",
            help="Mostra eventuali match con performance già esistenti, senza salvare nulla.",
        )

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
        match_dry_run = bool(options["match_dry_run"])

        self.stdout.write(self.style.SUCCESS("=== SCRUB TICKETMASTER NEW START ==="))
        self.stdout.write(f"Time: {timezone.now().isoformat()}")
        self.stdout.write(f"Dry-run: {dry_run}")
        self.stdout.write(
            f"Country: {country} | "
            f"months_ahead={months_ahead} "
            f"step_days={step_days} "
            f"size={size} "
            f"limit={limit or '∞'}"
        )

        from api.scrapers.ticketmaster_new import (
            iter_all_events_windowed,
            stable_checksum,
            TicketmasterError,
        )

        now = timezone.now()

        plat, _ = Piattaforma.objects.get_or_create(
            nome="ticketmaster",
            defaults={
                "dominio": "ticketmaster.it",
                "attivo": True,
            },
        )

        created_evt = 0
        updated_evt = 0

        created_perf = 0
        updated_perf = 0

        created_map = 0
        updated_map = 0
        skipped_unchanged_map = 0

        created_perf_map = 0
        updated_perf_map = 0
        unchanged_perf_map = 0

        perf_placeholder_midnight = 0
        perf_time_tbd_marked = 0

        processed = 0
        failed = 0

        # PATCH E — contatore finestre rate-limited per diagnostica.
        windows_rate_limited = 0

        events_iter = iter_all_events_windowed(
            country_code=country,
            months_ahead=months_ahead,
            step_days=step_days,
            size=size,
            include_tba=include_tba,
            include_tbd=include_tbd,
            source=source,
        )

        # PATCH E — wrapper per tracciare le finestre dall'esterno.
        # iter_all_events_windowed è un generatore flat; usiamo un contatore
        # interno al collector (debug_window=True) che stampa su print().
        # Qui aggiungiamo solo il catch delle eccezioni di rete a livello globale.

        try:
            i = 0
            while True:
                # PATCH D — next() esplicito per poter catturare TicketmasterError
                # lanciata dentro il generatore senza far crashare il comando.
                try:
                    e = next(events_iter)
                except StopIteration:
                    break
                except TicketmasterError as tm_err:
                    # PATCH D — errore di rete/rate limit dal collector:
                    # logghiamo su stderr e continuiamo (il generatore proverà
                    # la finestra successiva se disponibile, altrimenti StopIteration).
                    windows_rate_limited += 1
                    self.stderr.write(
                        self.style.ERROR(
                            f"[TM ERROR] TicketmasterError nel generatore: {tm_err} "
                            f"(windows_rate_limited finora={windows_rate_limited})"
                        )
                    )
                    # Il generatore è esausto dopo un'eccezione non handled al suo interno.
                    # Non possiamo riprendere: usciamo dal loop e andiamo al finally.
                    break
                except Exception as gen_err:
                    # PATCH D — eccezione generica imprevista dal generatore.
                    self.stderr.write(
                        self.style.ERROR(
                            f"[TM ERROR] Eccezione imprevista nel generatore: {gen_err}"
                        )
                    )
                    break

                i += 1

                if limit and i > limit:
                    break

                tm_id = e.get("id")

                if not tm_id:
                    continue

                name = e.get("name") or ""
                url = e.get("url") or ""

                dates_start = (e.get("dates", {}) or {}).get("start", {}) or {}
                dt_str = dates_start.get("dateTime")
                starts_at_utc = parse_dt_utc(dt_str)

                local_date, local_time = parse_local_date_time(e)

                venues = (e.get("_embedded", {}) or {}).get("venues", []) or []
                v0 = venues[0] if venues else {}

                venue_name = v0.get("name") or "Sconosciuto"
                city = (v0.get("city") or {}).get("name") or ""
                country_code_evt = (v0.get("country") or {}).get("countryCode") or country

                luogo_norm = slugify(f"{venue_name}-{city}-{country_code_evt}") or slugify(venue_name) or "luogo"
                evento_norm = slugify(name) or "evento"

                hash_canonico = sha256(f"ticketmaster:{tm_id}")
                slug_base = f"{evento_norm}-{tm_id[:8]}".strip("-")

                checksum = stable_checksum(e)

                if dry_run:
                    self.stdout.write(
                        f"[DRY] {tm_id} | {name} | starts_at_utc={starts_at_utc} | "
                        f"local={local_date} {local_time} | {venue_name} ({city},{country_code_evt})"
                    )

                    if match_dry_run:
                        match_starts = starts_at_utc

                        if match_starts is None and local_date:
                            match_starts = datetime.fromisoformat(local_date).replace(
                                hour=0,
                                minute=0,
                                second=0,
                                microsecond=0,
                                tzinfo=dt_tz.utc,
                            )

                        if not match_starts:
                            self.stdout.write(
                                self.style.WARNING(
                                    f"  [MATCH SKIP] data assente tm_id={tm_id} name={name}"
                                )
                            )

                        else:
                            matched_perf = find_best_matching_performance(
                                event_name=name,
                                starts_at_utc=match_starts,
                                city=city or None,
                                hours_window=12,
                                min_similarity=0.80,
                                max_time_diff_hours=2,
                            )

                            if matched_perf:
                                self.stdout.write(
                                    self.style.SUCCESS(
                                        f"  [MATCH FOUND] existing_perf={matched_perf.id} "
                                        f"existing_event={matched_perf.evento_id} "
                                        f"existing_name={matched_perf.evento.nome_evento} "
                                        f"existing_city={matched_perf.luogo.citta if matched_perf.luogo else '-'} "
                                        f"existing_date={matched_perf.starts_at_utc}"
                                    )
                                )

                            else:
                                self.stdout.write(
                                    self.style.WARNING(
                                        "  [MATCH MISS] nessuna performance esistente compatibile"
                                    )
                                )

                    processed += 1

                    if processed % progress_every == 0:
                        self.stdout.write(f"...progress(dry): {processed}")

                    continue

                try:
                    with transaction.atomic():
                        # --------------------------------------------------
                        # LUOGO
                        # --------------------------------------------------
                        luogo, _ = Luoghi.objects.get_or_create(
                            nome_normalizzato=luogo_norm,
                            defaults={
                                "nome": venue_name,
                                "indirizzo": v0.get("address", {}).get("line1"),
                                "citta": city or None,
                                "citta_normalizzata": slugify(city) if city else None,
                                "stato_iso": country_code_evt or None,
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

                        if country_code_evt and luogo.stato_iso != country_code_evt:
                            luogo.stato_iso = country_code_evt
                            changed_luogo = True

                        if changed_luogo:
                            luogo.save(
                                update_fields=[
                                    "nome",
                                    "citta",
                                    "stato_iso",
                                    "aggiornato_il",
                                ]
                            )

                        # --------------------------------------------------
                        # EVENTO
                        # --------------------------------------------------
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

                            desired_slug = self.unique_slug(slug_base, exclude_pk=evento.pk)

                            if desired_slug and evento.slug != desired_slug:
                                evento.slug = desired_slug
                                changed_evt = True

                            if changed_evt:
                                evento.save(
                                    update_fields=[
                                        "nome_evento",
                                        "nome_evento_normalizzato",
                                        "slug",
                                        "aggiornato_il",
                                    ]
                                )
                                updated_evt += 1

                        # --------------------------------------------------
                        # PERFORMANCE
                        # --------------------------------------------------
                        perf_starts = starts_at_utc
                        perf_status = "UNKNOWN"
                        perf_dispo = None

                        if perf_starts is None:
                            if local_date:
                                perf_starts = datetime.fromisoformat(local_date).replace(
                                    hour=0,
                                    minute=0,
                                    second=0,
                                    microsecond=0,
                                    tzinfo=dt_tz.utc,
                                )
                                perf_dispo = "time_tbd"
                                perf_placeholder_midnight += 1

                            else:
                                # Senza data non possiamo creare performance.
                                map_status = upsert_evento_piattaforma_safe(
                                    plat=plat,
                                    evento=evento,
                                    tm_id=tm_id,
                                    url=url,
                                    snapshot_raw=e,
                                    checksum=checksum,
                                    now=now,
                                )

                                if map_status == "created":
                                    created_map += 1
                                elif map_status == "updated":
                                    updated_map += 1
                                else:
                                    skipped_unchanged_map += 1

                                processed += 1
                                continue

                        matched_perf = find_best_matching_performance(
                            event_name=name,
                            starts_at_utc=perf_starts,
                            city=city or None,
                            hours_window=12,
                            min_similarity=0.80,
                        )

                        if matched_perf:
                            perf = matched_perf
                            perf_created = False

                            if perf.evento_id != evento.id:
                                evento = perf.evento

                        else:
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
                            changed_perf = False

                            if perf_dispo == "time_tbd":
                                if perf.disponibilita_agg != "time_tbd":
                                    perf.disponibilita_agg = "time_tbd"
                                    changed_perf = True

                                perf_time_tbd_marked += 1

                            if changed_perf:
                                perf.save(
                                    update_fields=[
                                        "disponibilita_agg",
                                        "aggiornato_il",
                                    ]
                                )

                        else:
                            changed_perf = False

                            if perf_dispo == "time_tbd" and perf.disponibilita_agg != "time_tbd":
                                perf.disponibilita_agg = "time_tbd"
                                changed_perf = True

                            if changed_perf:
                                perf.save(
                                    update_fields=[
                                        "disponibilita_agg",
                                        "aggiornato_il",
                                    ]
                                )
                                updated_perf += 1

                        # --------------------------------------------------
                        # MAPPING GENERALE EVENTO/PIATTAFORMA
                        # --------------------------------------------------
                        map_status = upsert_evento_piattaforma_safe(
                            plat=plat,
                            evento=evento,
                            tm_id=tm_id,
                            url=url,
                            snapshot_raw=e,
                            checksum=checksum,
                            now=now,
                        )

                        if map_status == "created":
                            created_map += 1
                        elif map_status == "updated":
                            updated_map += 1
                        else:
                            skipped_unchanged_map += 1

                        # --------------------------------------------------
                        # MAPPING SPECIFICO PERFORMANCE/PIATTAFORMA
                        # --------------------------------------------------
                        perf_map_status = upsert_performance_piattaforma_safe(
                            plat=plat,
                            perf=perf,
                            tm_id=tm_id,
                            url=url,
                            snapshot_raw=e,
                            checksum=checksum,
                            now=now,
                        )

                        if perf_map_status == "created":
                            created_perf_map += 1
                        elif perf_map_status == "updated":
                            updated_perf_map += 1
                        else:
                            unchanged_perf_map += 1

                    processed += 1

                except Exception as ex:
                    failed += 1
                    self.stderr.write(
                        self.style.WARNING(
                            f"[ERROR] tm_id={tm_id} name={name!r} err={ex}"
                        )
                    )

                if processed and processed % progress_every == 0:
                    self.stdout.write(
                        f"...progress: processed={processed} failed={failed} "
                        f"created_evt={created_evt} created_perf={created_perf} "
                        f"created_perf_map={created_perf_map} "
                        f"windows_rate_limited={windows_rate_limited}"  # PATCH E
                    )

        finally:
            # PATCH E — summary finestre aggiunto al report finale.
            self.stdout.write(
                self.style.SUCCESS(
                    "DB OK - "
                    f"processed={processed} failed={failed} | "
                    f"created eventi={created_evt}, performances={created_perf}, mappings={created_map} | "
                    f"updated eventi={updated_evt}, performances={updated_perf}, mappings={updated_map} | "
                    f"unchanged mappings={skipped_unchanged_map} | "
                    f"created perf_maps={created_perf_map}, updated perf_maps={updated_perf_map}, unchanged perf_maps={unchanged_perf_map} | "
                    f"perf placeholder-midnight={perf_placeholder_midnight} time_tbd_marked={perf_time_tbd_marked} | "
                    f"windows: rate_limited={windows_rate_limited}"  # PATCH E
                )
            )
            self.stdout.write(self.style.SUCCESS("=== SCRUB TICKETMASTER NEW END ==="))