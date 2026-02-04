# core/api/management/commands/scan_free_alerts.py

from __future__ import annotations

import time
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.core.mail import send_mail
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from api.models import (
    EventFollow,
    Listing,
    Rivendita,
    Notifica,
    Abbonamento,
    Monitoraggio,
)


def availability_for_event(event_id: int, now=None) -> tuple[int, int]:
    """
    Disponibilità sulla nostra piattaforma:
      - Listing ACTIVE non scaduti sulle performance dell'evento
      - Rivendite PUBLISHED disponibili sull'evento
    """
    now = now or timezone.now()

    listing_cnt = (
        Listing.objects
        .filter(performance__evento_id=event_id, status="ACTIVE")
        .filter(Q(expires_at__isnull=True) | Q(expires_at__gt=now))
        .count()
    )

    rivendita_cnt = (
        Rivendita.objects
        .filter(evento_id=event_id, disponibile=True, status="PUBLISHED")
        .count()
    )

    return listing_cnt, rivendita_cnt


def make_dedupe_key_free(user_id: int, event_id: int, bucket_minutes: int, now=None) -> str:
    """
    1 notifica ogni N minuti per utente+evento.
    """
    now = now or timezone.now()
    bucket = int(now.timestamp() // (bucket_minutes * 60))
    return f"FREE:{user_id}:{event_id}:{bucket_minutes}m:{bucket}"


def send_email_notification(to_email: str, subject: str, body: str) -> None:
    send_mail(
        subject=subject,
        message=body,
        from_email=None,          # usa DEFAULT_FROM_EMAIL se configurato
        recipient_list=[to_email],
        fail_silently=False,
    )


def get_or_create_free_monitoraggio(user, event):
    """
    Per i FREE (EventFollow) ci serve un Monitoraggio per poter salvare Notifica (FK obbligatoria).
    Se esiste già, lo riusa. Altrimenti lo crea usando un abbonamento attivo FREE dell'utente.
    """
    mon = (
        Monitoraggio.objects
        .filter(abbonamento__utente=user, evento=event)
        .order_by("-id")
        .first()
    )
    if mon:
        return mon

    # Abbonamento FREE attivo: plan NULL oppure plan.price=0 (come nel tuo DB)
    abb = (
        Abbonamento.objects
        .filter(utente=user, attivo=True)
        .filter(Q(plan__isnull=True) | Q(plan__price=0))
        .order_by("-data_inizio")
        .first()
    )
    if not abb:
        return None

    return Monitoraggio.objects.create(abbonamento=abb, evento=event)


class Command(BaseCommand):
    help = "Scan FREE alerts (EventFollow) -> se disponibilità, notifica (email) con dedupe e loop."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=500)
        parser.add_argument("--verbose", action="store_true")

        # loop ogni N secondi
        parser.add_argument("--loop", action="store_true")
        parser.add_argument("--sleep", type=int, default=5)

        # anti-spam: una notifica ogni N minuti per utente+evento
        parser.add_argument("--dedupe-minutes", type=int, default=15)

        # dry-run: non invia email e non salva Notifica
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        loop = options["loop"]
        sleep_s = max(1, int(options["sleep"]))
        if loop:
            self.stdout.write(self.style.WARNING(f"Loop attivo: scan ogni {sleep_s}s"))
            while True:
                self._run_once(**options)
                time.sleep(sleep_s)
        else:
            self._run_once(**options)

    def _run_once(self, **options):
        now = timezone.now()
        limit = options["limit"]
        verbose = options["verbose"]
        dedupe_minutes = int(options["dedupe_minutes"])
        dry_run = options["dry_run"]

        follows = (
            EventFollow.objects
            .select_related("user", "event")
            .order_by("id")[:limit]
        )

        self.stdout.write(self.style.SUCCESS(f"EventFollow trovati: {follows.count()}"))

        hits = 0
        sent = 0
        skipped = 0
        failed = 0

        for f in follows:
            user = f.user
            event = f.event

            listing_cnt, rivendita_cnt = availability_for_event(event.id, now=now)
            has_availability = (listing_cnt > 0) or (rivendita_cnt > 0)

            if not has_availability:
                if verbose:
                    self.stdout.write(
                        f"[--] user={user.email} event_id={event.id} nome='{event.nome_evento}' "
                        f"listings_active={listing_cnt} rivendite_pubbliche={rivendita_cnt}"
                    )
                continue

            hits += 1

            # canale email abilitato?
            if not getattr(user, "notify_email", True):
                skipped += 1
                if verbose:
                    self.stdout.write(f"[SKIP] notify_email=False user={user.email} event_id={event.id}")
                continue

            subject = f"Tixy: biglietti disponibili per '{event.nome_evento}'"
            body = (
                f"Ciao {user.first_name},\n\n"
                f"Sono disponibili biglietti sulla piattaforma per:\n"
                f"- Evento: {event.nome_evento}\n\n"
                f"Disponibilità attuale:\n"
                f"- Listing attivi: {listing_cnt}\n"
                f"- Rivendite pubbliche: {rivendita_cnt}\n\n"
                f"Accedi a Tixy per vedere i dettagli.\n\n"
                f"— Tixy"
            )

            # ✅ QUI IL FIX: prima creo/recupero Monitoraggio, poi faccio dedupe su Notifica
            mon = get_or_create_free_monitoraggio(user, event)
            if not mon:
                failed += 1
                self.stdout.write(
                    self.style.ERROR(
                        f"[ERR] Nessun abbonamento FREE attivo per creare Monitoraggio: user={user.email} event_id={event.id}"
                    )
                )
                continue

            dedupe_key = make_dedupe_key_free(user.id, event.id, bucket_minutes=dedupe_minutes, now=now)

            if Notifica.objects.filter(monitoraggio=mon, dedupe_key=dedupe_key, status="SENT").exists():
                skipped += 1
                if verbose:
                    self.stdout.write(
                        f"[SKIP] già notificato (dedupe) user={user.email} event_id={event.id} key={dedupe_key}"
                    )
                continue

            if dry_run:
                sent += 1
                self.stdout.write(
                    self.style.WARNING(
                        f"[DRY] invierei EMAIL a {user.email} event_id={event.id} "
                        f"listings_active={listing_cnt} rivendite_pubbliche={rivendita_cnt} key={dedupe_key}"
                    )
                )
                continue

            try:
                # transazione per evitare doppio invio in concorrenza
                with transaction.atomic():
                    # ricontrollo dedupe dentro transazione
                    if Notifica.objects.filter(monitoraggio=mon, dedupe_key=dedupe_key, status="SENT").exists():
                        skipped += 1
                        continue

                    send_email_notification(user.email, subject, body)

                    Notifica.objects.create(
                        monitoraggio=mon,
                        channel="email",
                        dedupe_key=dedupe_key,
                        status="SENT",
                        message=subject,
                    )

                sent += 1
                self.stdout.write(
                    self.style.SUCCESS(
                        f"[OK] EMAIL inviata user={user.email} event_id={event.id} "
                        f"listings_active={listing_cnt} rivendite_pubbliche={rivendita_cnt}"
                    )
                )

            except Exception as e:
                failed += 1
                self.stdout.write(self.style.ERROR(f"[ERR] invio fallito user={user.email} event_id={event.id}: {e}"))

        self.stdout.write(
            self.style.SUCCESS(
                f"Eventi con disponibilità: {hits} | inviate: {sent} | skip: {skipped} | fail: {failed}"
            )
        )
