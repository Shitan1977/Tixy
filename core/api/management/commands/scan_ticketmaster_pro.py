from __future__ import annotations

import random
import time
from typing import Optional, Tuple

import requests
from django.conf import settings
from django.core.mail import send_mail
from django.core.management.base import BaseCommand
from django.db import transaction
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
    if not getattr(ab, "attivo", False):
        return False
    data_fine = getattr(ab, "data_fine", None)
    if data_fine and data_fine < timezone.now():
        return False
    return True


def _has_internal_tickets(perf: Performance) -> bool:
    if Biglietto.objects.filter(performance=perf, is_valid=True).exists():
        return True
    if Listing.objects.filter(performance=perf, status="ACTIVE").exists():
        return True
    return False


def _dedupe_key(perf_id: int, user_id: int, platform: str, reason: str) -> str:
    day = timezone.now().date().isoformat()
    return f"{platform}:{reason}:perf:{perf_id}:user:{user_id}:{day}"


def _get_ticketmaster_mapping_for_performance(perf: Performance) -> Tuple[Optional[str], Optional[str], Optional[int]]:
    """
    Ritorna: (url, mapping_type, mapping_pk)
    mapping_type: "performance" | "evento" | None
    """
    pp = (
        PerformancePiattaforma.objects
        .filter(performance=perf, piattaforma__nome__iexact="ticketmaster")
        .exclude(url="")
        .order_by("-aggiornato_il")
        .first()
    )
    if pp and pp.url:
        return pp.url, "performance", pp.pk

    ep = (
        EventoPiattaforma.objects
        .filter(evento=perf.evento, piattaforma__nome__iexact="ticketmaster")
        .exclude(url="")
        .order_by("-aggiornato_il")
        .first()
    )
    if ep and ep.url:
        return ep.url, "evento", ep.pk

    return None, None, None


def _touch_last_scan(mapping_type: Optional[str], mapping_pk: Optional[int]) -> None:
    """Aggiorna ultima_scansione sul mapping usato."""
    if not mapping_type or not mapping_pk:
        return
    now = timezone.now()
    if mapping_type == "performance":
        PerformancePiattaforma.objects.filter(pk=mapping_pk).update(ultima_scansione=now)
    elif mapping_type == "evento":
        EventoPiattaforma.objects.filter(pk=mapping_pk).update(ultima_scansione=now)


def _sleep_with_jitter(base: float, *, heavy: bool = False) -> None:
    j = random.uniform(0.15, 0.85)
    extra = random.uniform(2.0, 6.0) if heavy else 0.0
    time.sleep(max(0.0, base + j + extra))


def _send_email_with_retry(*, subject: str, message: str, to_email: str, max_retries: int, base_wait: float) -> Tuple[bool, str]:
    """
    Riprova l'invio email con backoff.
    Ritorna: (ok, last_error)
    """
    last_err = ""
    for attempt in range(1, max_retries + 1):
        try:
            send_mail(
                subject=subject,
                message=message,
                from_email=getattr(settings, "DEFAULT_FROM_EMAIL", None),
                recipient_list=[to_email],
                fail_silently=False,
            )
            return True, ""
        except Exception as e:
            last_err = str(e)
            # backoff crescente (attempt 1 -> base_wait, attempt 2 -> 2*base_wait, ...)
            wait = base_wait * attempt
            time.sleep(wait)
    return False, last_err


# -------------------------
# Command
# -------------------------

class Command(BaseCommand):
    help = "Scansiona monitoraggi PRO attivi e invia email quando Ticketmaster torna disponibile."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=200, help="Quanti monitoraggi massimo processare.")
        parser.add_argument("--sleep", type=float, default=0.35, help="Pausa base tra chiamate esterne (anti-ban).")
        parser.add_argument("--dry-run", action="store_true", help="Non invia email e non salva Notifica.")
        parser.add_argument("--verbose", action="store_true", help="Log più dettagliato.")

        parser.add_argument("--email-retries", type=int, default=3, help="Quanti tentativi per inviare email.")
        parser.add_argument("--email-wait", type=float, default=1.5, help="Attesa base tra tentativi email (backoff).")

    def handle(self, *args, **opts):
        limit = int(opts["limit"])
        sleep_s = float(opts["sleep"])
        dry_run = bool(opts["dry_run"])
        verbose = bool(opts["verbose"])
        email_retries = max(1, int(opts["email_retries"]))
        email_wait = max(0.5, float(opts["email_wait"]))

        now = timezone.now()

        # Sessione HTTP riusata
        tm_session = requests.Session()
        tm_session.headers.update({
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome Safari",
            "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
        })

        # Query monitoraggi PRO attivi
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
            .order_by("id")
        )

        if verbose:
            self.stdout.write(f"[DEBUG] now={now.isoformat()} qs_count={qs.count()}")

        # Filtro extra python (sicuro) + limite
        monitoraggi = []
        for m in qs[: limit * 5]:
            try:
                if _abbonamento_is_active(m.abbonamento):
                    monitoraggi.append(m)
            except Exception:
                # se qualche record è corrotto, non blocchiamo
                continue
            if len(monitoraggi) >= limit:
                break

        self.stdout.write(f"[SCAN] monitoraggi PRO attivi: {len(monitoraggi)} (limit={limit})")

        counters = {
            "processed": 0,
            "notified": 0,
            "skip_no_perf": 0,
            "skip_internal": 0,
            "skip_no_mapping": 0,
            "skip_not_avail": 0,
            "skip_dedup": 0,
            "tm_error": 0,
            "email_fail": 0,
            "no_email_pref": 0,
        }

        for m in monitoraggi:
            counters["processed"] += 1

            # isoliamo ogni monitoraggio: se uno esplode non ferma il job
            try:
                user = m.abbonamento.utente
                perf = m.performance
                if perf is None:
                    counters["skip_no_perf"] += 1
                    if verbose:
                        self.stdout.write(f"[SKIP] monitoraggio {m.id}: performance mancante")
                    continue

                # se già abbiamo biglietti interni: niente alert
                if _has_internal_tickets(perf):
                    counters["skip_internal"] += 1
                    if verbose:
                        self.stdout.write(f"[SKIP] perf {perf.id}: biglietti già presenti (DB/listing)")
                    continue

                # mapping Ticketmaster (performance -> fallback evento)
                tm_url, mapping_type, mapping_pk = _get_ticketmaster_mapping_for_performance(perf)
                if not tm_url:
                    counters["skip_no_mapping"] += 1
                    if verbose:
                        self.stdout.write(f"[SKIP] perf {perf.id}: mapping TM assente")
                    continue

                # scan Ticketmaster
                try:
                    res = check_ticketmaster_page_availability(url=tm_url, session=tm_session)
                except Exception as ex:
                    counters["tm_error"] += 1
                    if verbose:
                        self.stdout.write(f"[TM EXC] perf {perf.id} url={tm_url} err={ex}")
                    _touch_last_scan(mapping_type, mapping_pk)
                    _sleep_with_jitter(sleep_s, heavy=True)
                    continue

                # aggiorniamo SEMPRE ultima_scansione del mapping usato
                _touch_last_scan(mapping_type, mapping_pk)

                if not res.get("ok"):
                    counters["tm_error"] += 1
                    sc = res.get("status_code")
                    if verbose:
                        self.stdout.write(f"[TM ERR] perf {perf.id} status={sc} reason={res.get('reason')}")
                    if sc in (403, 429):
                        _sleep_with_jitter(sleep_s, heavy=True)
                    else:
                        _sleep_with_jitter(sleep_s)
                    continue

                availability = res.get("availability")
                if availability != "available":
                    counters["skip_not_avail"] += 1
                    if verbose:
                        self.stdout.write(f"[TM] perf {perf.id} => {availability} ({res.get('reason')})")
                    _sleep_with_jitter(sleep_s)
                    continue

                # dedupe SOLO su notifiche SENT
                dk = _dedupe_key(perf.id, user.id, "ticketmaster", "BACK_IN_STOCK")
                if Notifica.objects.filter(dedupe_key=dk, status="SENT").exists():
                    counters["skip_dedup"] += 1
                    if verbose:
                        self.stdout.write(f"[DEDUP] perf {perf.id} già notificata oggi (SENT)")
                    _sleep_with_jitter(sleep_s)
                    continue

                # preferenze email utente
                if not getattr(user, "notify_email", True):
                    counters["no_email_pref"] += 1
                    if verbose:
                        self.stdout.write(f"[NO EMAIL PREF] user={getattr(user,'email',None)}")
                    _sleep_with_jitter(sleep_s)
                    continue

                # email content
                event_title = perf.evento.nome_evento if getattr(perf, "evento_id", None) else "Evento"
                luogo = getattr(perf.luogo, "nome", "") if getattr(perf, "luogo_id", None) else ""
                when = perf.starts_at_utc.isoformat() if getattr(perf, "starts_at_utc", None) else "—"

                subject = f"Biglietti disponibili: {event_title}"
                msg = (
                    f"Ciao {getattr(user,'first_name','')},\n\n"
                    f"Sono tornati disponibili biglietti su Ticketmaster per:\n"
                    f"- Evento: {event_title}\n"
                    f"- Luogo: {luogo}\n"
                    f"- Data: {when}\n\n"
                    f"Link: {tm_url}\n\n"
                    f"— Tixy"
                )

                if dry_run:
                    self.stdout.write(f"[DRY] WOULD EMAIL user={user.email} perf={perf.id} url={tm_url}")
                    counters["notified"] += 1
                    _sleep_with_jitter(sleep_s)
                    continue

                # invio email + retry, e SALVO Notifica solo se OK
                ok, err = _send_email_with_retry(
                    subject=subject,
                    message=msg,
                    to_email=user.email,
                    max_retries=email_retries,
                    base_wait=email_wait,
                )

                if not ok:
                    counters["email_fail"] += 1
                    self.stdout.write(f"[EMAIL FAIL] {user.email} perf={perf.id} last_err={err}")
                    # NON creo Notifica SENT -> così il job potrà riprovare in future run
                    _sleep_with_jitter(sleep_s)
                    continue

                # salva Notifica dopo successo (dedupe reale)
                with transaction.atomic():
                    Notifica.objects.create(
                        monitoraggio=m,
                        channel="email",
                        dedupe_key=dk,
                        status="SENT",
                        sent_at=timezone.now(),
                        message=msg,
                    )

                counters["notified"] += 1
                self.stdout.write(f"[EMAIL OK] {user.email} perf={perf.id}")

                _sleep_with_jitter(sleep_s)

            except Exception as ex:
                # qualsiasi eccezione non deve mai fermare il cron
                self.stdout.write(f"[FATAL-SKIP] monitoraggio={getattr(m,'id',None)} err={ex}")
                _sleep_with_jitter(sleep_s, heavy=True)
                continue

        self.stdout.write(
            "[DONE] "
            f"processed={counters['processed']} notified={counters['notified']} "
            f"skip_no_perf={counters['skip_no_perf']} skip_internal={counters['skip_internal']} "
            f"skip_no_mapping={counters['skip_no_mapping']} skip_not_avail={counters['skip_not_avail']} "
            f"skip_dedup={counters['skip_dedup']} tm_error={counters['tm_error']} "
            f"email_fail={counters['email_fail']} no_email_pref={counters['no_email_pref']}"
        )
