"""
Manutenzione del marketplace (da schedulare ogni ~5 minuti via cron/Celery beat):

1. Annulla gli ordini PENDING più vecchi di TIXY_PENDING_ORDER_TTL_MINUTES
   (default 30) e ripristina la disponibilità del listing (qty + eventuale
   RESERVED -> ACTIVE). Senza questo, un checkout abbandonato blocca
   l'annuncio per sempre.

2. Marca EXPIRED i listing ACTIVE/RESERVED la cui performance è già passata.

Uso:
    python manage.py expire_orders
    python manage.py expire_orders --pending-minutes 15 --dry-run
"""
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from api.models import Listing, OrderTicket


class Command(BaseCommand):
    help = "Annulla ordini PENDING scaduti (ripristinando la disponibilità) e marca EXPIRED i listing di eventi passati"

    def add_arguments(self, parser):
        parser.add_argument(
            "--pending-minutes",
            type=int,
            default=int(getattr(settings, "TIXY_PENDING_ORDER_TTL_MINUTES", 30)),
            help="Minuti dopo i quali un ordine PENDING non pagato viene annullato",
        )
        parser.add_argument("--dry-run", action="store_true", help="Mostra cosa verrebbe fatto senza scrivere")

    def handle(self, *args, **options):
        now = timezone.now()
        dry_run = options["dry_run"]
        cutoff = now - timedelta(minutes=options["pending_minutes"])

        # --- 1) Ordini PENDING scaduti ---
        expired_ids = list(
            OrderTicket.objects
            .filter(status="PENDING", created_at__lt=cutoff)
            .values_list("id", flat=True)
        )
        cancelled = 0
        for order_id in expired_ids:
            if dry_run:
                self.stdout.write(f"[dry-run] annullerei ordine PENDING #{order_id}")
                cancelled += 1
                continue
            try:
                with transaction.atomic():
                    order = OrderTicket.objects.select_for_update().get(pk=order_id)
                    if order.status != "PENDING":
                        continue  # pagato/annullato nel frattempo
                    listing = Listing.objects.select_for_update().get(pk=order.listing_id)

                    order.status = "CANCELLED"
                    order.save(update_fields=["status"])

                    # ripristina la quantità riservata dal checkout
                    listing.qty = (listing.qty or 0) + (order.qty or 0)
                    update_fields = ["qty", "updated_at"]
                    perf = listing.performance
                    event_future = bool(perf and perf.starts_at_utc and perf.starts_at_utc > now)
                    if listing.status == "RESERVED" and listing.qty > 0 and event_future:
                        listing.status = "ACTIVE"
                        update_fields.append("status")
                    listing.save(update_fields=update_fields)
                cancelled += 1
                self.stdout.write(f"ordine #{order_id} annullato, disponibilità ripristinata")
            except Exception as e:
                self.stderr.write(f"errore su ordine #{order_id}: {e}")

        # --- 2) Listing di eventi passati -> EXPIRED ---
        stale_qs = Listing.objects.filter(
            status__in=["ACTIVE", "RESERVED"],
            performance__starts_at_utc__lt=now,
        )
        if dry_run:
            expired_listings = stale_qs.count()
            self.stdout.write(f"[dry-run] marcherei EXPIRED {expired_listings} listing")
        else:
            expired_listings = stale_qs.update(status="EXPIRED", updated_at=now)

        self.stdout.write(self.style.SUCCESS(
            f"Fatto: {cancelled} ordini PENDING annullati, {expired_listings} listing scaduti (EXPIRED)."
        ))
