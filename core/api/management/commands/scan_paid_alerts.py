from __future__ import annotations

import time
from typing import Optional
import random
import requests
from django.conf import settings
from django.core.mail import send_mail
from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from api.models import (
    Abbonamento,
    Monitoraggio,
    Performance,
    PerformancePiattaforma,
    EventoPiattaforma,
    Biglietto,
    Listing,
    Notifica,
)

from api.scrapers.ticketmaster_availability import check_ticketmaster_page_availability


# -------------------------
# Helpers
# -------------------------

def _abbonamento_is_active(ab: Abbonamento) -> bool:
    """
    Un abbonamento è attivo se:
    - attivo=True
    - data_fine è None oppure >= now
    """
    if not ab.attivo:
        return False
    if ab.data_fine and ab.data_fine < timezone.now():
        return False
    return True


def _get_ticketmaster_url_for_performance(perf: Performance) -> Optional[str]:
    """
    Recupera la URL Ticketmaster per una performance.

    Strategia:
    1) prova PerformancePiattaforma (se in futuro lo popoli)
    2) fallback EventoPiattaforma (quello che hai davvero oggi)
    """
    pp = (
        PerformancePiattaforma.objects
        .filter(performance=perf, piattaforma__nome="ticketmaster")
        .exclude(url="")
        .order_by("-aggiornato_il")
        .first()
    )
    if pp and pp.url:
        return pp.url

    ep = (
        EventoPiattaforma.objects
        .filter(evento=perf.evento, piattaforma__nome="ticketmaster")
        .exclude(url="")
        .order_by("-aggiornato_il")
        .first()
    )
    if ep and ep.url:
        return ep.url

    return None


def _has_internal_tickets(perf: Performance) -> bool:
    """
    Se abbiamo già biglietti interni per quella performance, non ha senso avvisare.
    - Biglietto validi in DB
    - oppure Listing attivi in marketplace
    """
    if Biglietto.objects.filter(performance=perf, is_valid=True).exists():
        return True
    if Listing.objects.filter(performance=perf, status="ACTIVE").exists():
        return True
    return False


def _dedupe_key(perf_id: int, user_id: int, platform: str, reason: str) -> str:
    """
    1 notifica al giorno per performance + user + reason + platform.
    """
    day = timezone.now().date().isoformat()
    return f"{platform}:{reason}:perf:{perf_id}:user:{user_id}:{day}"



# -------------------------
# Command
# -------------------------

class Command(BaseCommand):
    help = "Scansiona monitoraggi PRO (abbonamenti attivi) e invia email quando Ticketmaster torna disponibile."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=100, help="Quanti monitoraggi massimo processare.")
        parser.add_argument("--sleep", type=float, default=0.4, help="Sleep tra chiamate esterne (anti-ban).")
        parser.add_argument("--dry-run", action="store_true", help="Non invia email e non salva Notifica.")
        parser.add_argument("--verbose", action="store_true", help="Log più dettagliato.")

    def handle(self, *args, **opts):
        limit = opts["limit"]
        sleep_s = opts["sleep"]
        dry_run = opts["dry_run"]
        verbose = opts["verbose"]

        now = timezone.now()
        # --- Ticketmaster: sessione riusata (cookie/keep-alive) + jitter ---
        tm_session = requests.Session()

        def snooze(base: float, *, heavy: bool = False) -> None:
            # jitter per evitare pattern fisso
            j = random.uniform(0.2, 0.9)
            extra = random.uniform(2.0, 6.0) if heavy else 0.0
            time.sleep(max(0.0, base + j + extra))

        # Monitoraggi legati a piani a pagamento (plan presente e price>0)
        qs = (
            Monitoraggio.objects
            .filter(
                abbonamento__attivo=True,
                abbonamento__prezzo__gt=0,
            )
            .filter(Q(abbonamento__data_fine__isnull=True) | Q(abbonamento__data_fine__gte=now))
            .select_related(
                "abbonamento",
                "abbonamento__utente",
                "performance",
                "performance__evento",
                "performance__luogo",
            )
        )

        if verbose:
            self.stdout.write(f"[DEBUG] now={now}")
            self.stdout.write(f"[DEBUG] monitoraggi_qs_count={qs.count()}")

        # filtro python extra con helper (ridondante ma sicuro)
        monitoraggi = []
        for m in qs[: limit * 5]:
            if _abbonamento_is_active(m.abbonamento):
                monitoraggi.append(m)
            if len(monitoraggi) >= limit:
                break

        self.stdout.write(f"[SCAN] monitoraggi attivi trovati: {len(monitoraggi)} (limit={limit})")

        done = 0
        notified = 0
        skipped_no_perf = 0
        skipped_has_internal = 0
        skipped_no_mapping = 0
        skipped_not_available = 0
        skipped_deduped = 0
        skipped_tm_error = 0

        for m in monitoraggi:
            done += 1
            user = m.abbonamento.utente

            perf = m.performance
            if perf is None:
                skipped_no_perf += 1
                if verbose:
                    self.stdout.write(f"[SKIP] monitoraggio {m.id}: nessuna performance (evento-only)")
                continue

            # Se abbiamo già biglietti interni, non avvisiamo
            if _has_internal_tickets(perf):
                skipped_has_internal += 1
                if verbose:
                    self.stdout.write(f"[SKIP] perf {perf.id}: già biglietti nel DB/listing (no alert)")
                continue

            # URL ticketmaster (perf->evento fallback)
            tm_url = _get_ticketmaster_url_for_performance(perf)
            if not tm_url:
                skipped_no_mapping += 1
                if verbose:
                    self.stdout.write(f"[SKIP] perf {perf.id}: nessun mapping Ticketmaster url")
                continue

            # Scan Ticketmaster
            res = check_ticketmaster_page_availability(url=tm_url, session=tm_session)


            # se ok=False è un errore di fetch (timeout, block, ecc)
            if not res.get("ok"):
                skipped_tm_error += 1
                sc = res.get("status_code")
                if verbose:
                    self.stdout.write(f"[TM ERR] perf {perf.id} {tm_url} => {res.get('reason')} (status={sc})")

                # se 403/429: cooldown più lungo per non farti bloccare peggio
                if sc in (403, 429):
                    snooze(sleep_s, heavy=True)
                else:
                    snooze(sleep_s)

                continue

            availability = res.get("availability")
            if availability != "available":
                skipped_not_available += 1
                if verbose:
                    self.stdout.write(f"[TM] perf {perf.id} => {availability} ({res.get('reason')})")
                snooze(sleep_s)
                continue

            # Dedupe: 1 notifica al giorno
            dk = _dedupe_key(perf.id, user.id, "ticketmaster", "BACK_IN_STOCK")

            if Notifica.objects.filter(dedupe_key=dk).exists():
                skipped_deduped += 1
                if verbose:
                    self.stdout.write(f"[DEDUP] perf {perf.id} già notificata oggi")
                snooze(sleep_s)
                continue

            # Messaggio email
            event_title = perf.evento.nome_evento if perf.evento_id else "Evento"
            luogo = getattr(perf.luogo, "nome", "") if getattr(perf, "luogo_id", None) else ""
            when = perf.starts_at_utc.isoformat() if perf.starts_at_utc else "—"

            subject = f"Biglietti disponibili: {event_title}"
            msg = (
                f"Ciao {user.first_name},\n\n"
                f"Sono tornati disponibili biglietti su Ticketmaster per:\n"
                f"- Evento: {event_title}\n"
                f"- Luogo: {luogo}\n"
                f"- Data: {when}\n\n"
                f"Link: {tm_url}\n\n"
                f"— Tixy"
            )

            if dry_run:
                self.stdout.write(f"[DRY] NOTIFY user={user.email} perf={perf.id} url={tm_url}")
                notified += 1
                snooze(sleep_s)
                continue


            # Invio email se consentito
            # Invio email; salvo Notifica SOLO dopo successo (dedupe)
            if getattr(user, "notify_email", True):
                try:
                    send_mail(
                        subject=subject,
                        message=msg,
                        from_email=getattr(settings, "DEFAULT_FROM_EMAIL", None),
                        recipient_list=[user.email],
                        fail_silently=False,
                    )
                except Exception as e:
                    self.stdout.write(f"[EMAIL FAIL] {user.email}: {e}")
                else:
                    Notifica.objects.create(
                        monitoraggio=m,
                        channel="email",
                        dedupe_key=dk,
                        status="SENT",
                        sent_at=now,
                        message=msg,
                    )
                    self.stdout.write(f"[EMAIL OK] {user.email} perf={perf.id}")
            else:
                self.stdout.write(f"[NO EMAIL PREF] user={user.email}")

            notified += 1
            snooze(sleep_s)

        self.stdout.write(
            f"[DONE] processed={done} notified={notified} "
            f"skip_no_perf={skipped_no_perf} skip_internal={skipped_has_internal} "
            f"skip_no_mapping={skipped_no_mapping} skip_not_avail={skipped_not_available} "
            f"skip_tm_error={skipped_tm_error} skip_dedup={skipped_deduped}"
        )
