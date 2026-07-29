"""
Manutenzione del marketplace (da schedulare ogni ~5 minuti via cron/Celery beat):

1. Annulla gli ordini PENDING più vecchi di TIXY_PENDING_ORDER_TTL_MINUTES
   (default 30) e ripristina la disponibilità del listing (qty + eventuale
   RESERVED -> ACTIVE). Senza questo, un checkout abbandonato blocca
   l'annuncio per sempre.

2. Marca EXPIRED i listing ACTIVE/RESERVED la cui performance è già passata.

3. Annulla e rimborsa gli ordini PAID il cui venditore non ha caricato il
   biglietto rinominato entro TIXY_SELLER_UPLOAD_DEADLINE_HOURS (default 12)
   da paid_at: libera i TicketSubitem presi dall'ordine, rimette in vendita
   il listing e notifica acquirente/venditore. Senza questo, un venditore che
   non consegna lascia l'ordine bloccato "pagato" per sempre.

Uso:
    python manage.py expire_orders
    python manage.py expire_orders --pending-minutes 15 --upload-deadline-hours 12 --dry-run
"""
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from api.models import Listing, OrderTicket, TicketSubitem
from api.notifications import notify_user_push
from api.utils import invia_email_acquirente_ordine_annullato, invia_email_venditore_ordine_annullato


class Command(BaseCommand):
    help = "Annulla ordini PENDING scaduti, marca EXPIRED i listing di eventi passati, e annulla/rimborsa gli ordini PAID non consegnati in tempo"

    def add_arguments(self, parser):
        parser.add_argument(
            "--pending-minutes",
            type=int,
            default=int(getattr(settings, "TIXY_PENDING_ORDER_TTL_MINUTES", 30)),
            help="Minuti dopo i quali un ordine PENDING non pagato viene annullato",
        )
        parser.add_argument(
            "--upload-deadline-hours",
            type=int,
            default=int(getattr(settings, "TIXY_SELLER_UPLOAD_DEADLINE_HOURS", 12)),
            help="Ore dopo paid_at entro cui il venditore deve caricare il biglietto rinominato",
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

        # --- 3) Ordini PAID il cui venditore non ha consegnato in tempo ---
        upload_cutoff = now - timedelta(hours=options["upload_deadline_hours"])
        stale_paid_ids = list(
            OrderTicket.objects
            .filter(status="PAID", paid_at__lt=upload_cutoff)
            .values_list("id", flat=True)
        )
        refunded = 0
        for order_id in stale_paid_ids:
            if dry_run:
                self.stdout.write(f"[dry-run] annullerei e rimborserei ordine PAID #{order_id} (upload scaduto)")
                refunded += 1
                continue
            try:
                with transaction.atomic():
                    order = OrderTicket.objects.select_for_update().get(pk=order_id)
                    if order.status != "PAID":
                        continue  # consegnato/gestito nel frattempo
                    listing = Listing.objects.select_for_update().get(pk=order.listing_id)

                    # libera i sub-biglietti presi da questo ordine, così tornano disponibili
                    TicketSubitem.objects.filter(sold_order=order).update(is_sold=False, sold_order=None)

                    order.status = "REFUNDED"
                    order.save(update_fields=["status"])

                    remaining_unsold = listing.subitems.filter(subitem__is_sold=False).count()
                    listing.qty = remaining_unsold
                    update_fields = ["qty", "updated_at"]
                    perf = listing.performance
                    event_future = bool(perf and perf.starts_at_utc and perf.starts_at_utc > now)
                    if remaining_unsold > 0 and event_future and listing.status in ("SOLD", "RESERVED"):
                        listing.status = "ACTIVE"
                        update_fields.append("status")
                    listing.save(update_fields=update_fields)

                try:
                    invia_email_acquirente_ordine_annullato(order)
                    invia_email_venditore_ordine_annullato(order)
                except Exception:
                    pass
                notify_user_push(
                    order.buyer, "Ordine annullato e rimborsato",
                    f"Ordine #{order.id}: il venditore non ha consegnato in tempo, verrai rimborsato.",
                    {"type": "order_refunded", "order_id": order.id},
                )
                notify_user_push(
                    listing.seller, "Vendita annullata",
                    f"Ordine #{order.id} annullato per mancata consegna: il tuo annuncio è di nuovo in vendita.",
                    {"type": "order_refunded", "order_id": order.id},
                )
                refunded += 1
                self.stdout.write(f"ordine #{order_id} annullato e rimborsato, listing rimesso in vendita")
            except Exception as e:
                self.stderr.write(f"errore su ordine PAID #{order_id}: {e}")

        self.stdout.write(self.style.SUCCESS(
            f"Fatto: {cancelled} ordini PENDING annullati, {expired_listings} listing scaduti (EXPIRED), "
            f"{refunded} ordini PAID annullati/rimborsati per mancata consegna."
        ))
