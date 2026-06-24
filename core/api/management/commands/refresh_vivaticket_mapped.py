"""
refresh_vivaticket_mapped.py — Rinfresca lo snapshot_raw dei PerformancePiattaforma
Vivaticket collegati a monitoraggi PRO attivi, chiamando direttamente
get_vivaticket_event_detail_from_url (scraping live).

Motivazione: scrub_vivaticket_events aggiorna solo gli eventi presenti nella
pagina lista "biglietti-musica" (primi N). Gli eventi mappati ma non in lista
restano con snapshot stale. Questo comando garantisce che OGNI evento monitorato
da un cliente PRO venga rinfrescato, indipendentemente dalla lista.

Aggiorna nello snapshot: sale_status, is_sell_active, shop_type, pcode, tcode,
performance_status, buttons (se presenti). Non tocca altri campi.
"""

import time

from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone


# Campi che rinfreschiamo dallo scraping live
REFRESH_FIELDS = [
    "sale_status",
    "is_sell_active",
    "shop_type",
    "pcode",
    "tcode",
    "performance_status",
    "resale_link",
    "resale_active",
]


class Command(BaseCommand):
    help = "Rinfresca snapshot_raw Vivaticket per i mapping collegati a monitoraggi PRO attivi."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=200,
                            help="Numero massimo di PerformancePiattaforma da rinfrescare.")
        parser.add_argument("--sleep", type=float, default=1.0,
                            help="Pausa (secondi) tra una chiamata e l'altra.")
        parser.add_argument("--verbose", action="store_true",
                            help="Log dettagliato.")
        parser.add_argument("--dry-run", action="store_true",
                            help="Non salva, mostra solo cosa farebbe.")
        parser.add_argument("--only-id", type=int, default=None,
                            help="Rinfresca solo il PerformancePiattaforma con questo id.")
        parser.add_argument("--all-mapped", action="store_true",
                            help="Rinfresca TUTTI i PP Vivaticket mappati, non solo quelli con monitoraggi PRO attivi.")

    def handle(self, *args, **options):
        limit = options["limit"]
        sleep_seconds = options["sleep"]
        verbose = options["verbose"]
        dry_run = options["dry_run"]
        only_id = options.get("only_id")
        all_mapped = options.get("all_mapped", False)

        from api.models import PerformancePiattaforma, Monitoraggio
        from api.scrapers.vivaticket.client import get_vivaticket_event_detail_from_url

        now = timezone.now()
        self.stdout.write(self.style.SUCCESS("[START] refresh_vivaticket_mapped"))
        self.stdout.write(f"[TIME] {now.isoformat()}")
        self.stdout.write(f"[CONFIG] limit={limit} sleep={sleep_seconds} dry_run={dry_run} all_mapped={all_mapped}")

        qs = (
            PerformancePiattaforma.objects
            .select_related("piattaforma", "performance")
            .filter(piattaforma__nome__iexact="vivaticket")
        )

        if only_id:
            qs = qs.filter(id=only_id)
        elif not all_mapped:
            # Solo i PP collegati a una performance con monitoraggio PRO attivo
            perf_ids = (
                Monitoraggio.objects
                .filter(abbonamento__attivo=True, abbonamento__prezzo__gt=0)
                .filter(Q(abbonamento__data_fine__isnull=True) | Q(abbonamento__data_fine__gte=now))
                .values_list("performance_id", flat=True)
            )
            perf_ids = set(p for p in perf_ids if p)
            qs = qs.filter(performance_id__in=perf_ids)

        qs = qs.order_by("id")

        total = qs.count()
        self.stdout.write(f"[TARGET] PerformancePiattaforma Vivaticket da rinfrescare: {total}")

        processed = 0
        updated = 0
        no_data = 0
        no_url = 0
        changed_status = 0
        errors = 0

        for pp in qs:
            if processed >= limit:
                break

            url = (pp.url or "").strip()
            # Per il refresh serve l'URL pagina pubblica vivaticket.com/it/ticket/...
            # Se l'URL e' shop.vivaticket.com, proviamo comunque (la funzione gestisce entrambi).
            if not url:
                no_url += 1
                if verbose:
                    self.stdout.write(self.style.WARNING(f"[SKIP] pp_id={pp.id}: url vuoto"))
                continue

            processed += 1

            try:
                data = get_vivaticket_event_detail_from_url(url)
            except Exception as exc:
                errors += 1
                self.stdout.write(self.style.ERROR(f"[ERROR] pp_id={pp.id} url={url[:60]} exc={exc}"))
                if sleep_seconds > 0:
                    time.sleep(sleep_seconds)
                continue

            if not data:
                no_data += 1
                if verbose:
                    self.stdout.write(self.style.WARNING(f"[NO DATA] pp_id={pp.id} url={url[:60]}"))
                if sleep_seconds > 0:
                    time.sleep(sleep_seconds)
                continue

            snap = dict(pp.snapshot_raw or {})
            old_sale_status = snap.get("sale_status")

            # Aggiorna solo i campi di refresh, preservando il resto
            for field in REFRESH_FIELDS:
                if field in data and data.get(field) is not None:
                    snap[field] = data.get(field)

            # buttons: aggiorna se presenti nei dati freschi
            if data.get("buttons"):
                snap["buttons"] = data.get("buttons")

            new_sale_status = snap.get("sale_status")
            status_changed = (old_sale_status != new_sale_status)
            if status_changed:
                changed_status += 1

            if verbose or status_changed:
                marker = " [CHANGED]" if status_changed else ""
                self.stdout.write(
                    f"[REFRESH] pp_id={pp.id} sale_status: {old_sale_status} -> {new_sale_status} "
                    f"is_sell_active={snap.get('is_sell_active')}{marker}"
                )

            if not dry_run:
                pp.snapshot_raw = snap
                pp.ultima_scansione = timezone.now()
                pp.save(update_fields=["snapshot_raw", "ultima_scansione"])
                updated += 1

            if sleep_seconds > 0:
                time.sleep(sleep_seconds)

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("[DONE]"))
        self.stdout.write(f"processed       = {processed}")
        self.stdout.write(f"updated         = {updated}")
        self.stdout.write(f"changed_status  = {changed_status}")
        self.stdout.write(f"no_data         = {no_data}")
        self.stdout.write(f"no_url          = {no_url}")
        self.stdout.write(f"errors          = {errors}")
