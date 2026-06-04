from __future__ import annotations

# =============================================================================
# scan_ticketone_pro_alerts.py — VERSIONE DEFINITIVA
# =============================================================================
# PATCH 1 — Rimosso [TEST Tixy] dal subject email → sempre [Tixy]
# PATCH 2 — Aggiunto --only-email per test controllati su singolo utente
# PATCH 3 — Aggiunto skip performance già passate
# PATCH 4 — Aggiunto --skip-scan-hours anti rate-limit
# PATCH 5 — Rimosso parametro forced da _build_email_message
# PATCH 6 — Aggiunto contatore skip_past nel riepilogo finale
# =============================================================================

import time
from decimal import Decimal, InvalidOperation
from typing import Optional, Tuple, Dict, Any

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

from api.scrapers.ticketone.ticketone_prices import get_ticketone_price_data

SKIP_IF_SCANNED_WITHIN_HOURS: int = 4


# =============================================================================
# Helpers generici
# =============================================================================

def _abbonamento_is_active(ab: Abbonamento) -> bool:
    """
    Controlla se l'abbonamento è realmente attivo.
    Verifica anche data_fine perché esistono abbonamenti con attivo=True
    ma data_fine già scaduta (bug di consistenza DB).
    """
    if not getattr(ab, "attivo", False):
        return False
    data_fine = getattr(ab, "data_fine", None)
    if data_fine and data_fine < timezone.now():
        return False
    return True


def _has_internal_tickets(perf: Performance) -> bool:
    """Se esistono biglietti interni validi o listing attivi, non serve alert esterno."""
    if Biglietto.objects.filter(performance=perf, is_valid=True).exists():
        return True
    if Listing.objects.filter(performance=perf, status="ACTIVE").exists():
        return True
    return False


def _performance_is_past(perf: Performance) -> bool:
    """PATCH 3: salta performance già passate."""
    starts_at = getattr(perf, "starts_at_utc", None)
    if starts_at and starts_at < timezone.now():
        return True
    return False


def _dedupe_key(perf_id: int, user_id: int, platform: str, reason: str) -> str:
    """Chiave giornaliera per evitare più email uguali nello stesso giorno."""
    day = timezone.now().date().isoformat()
    return f"{platform}:{reason}:perf:{perf_id}:user:{user_id}:{day}"


def _to_decimal(value) -> Optional[Decimal]:
    """Converte un prezzo in Decimal in modo sicuro."""
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _send_email_with_retry(
    *,
    subject: str,
    message: str,
    to_email: str,
    max_retries: int,
    base_wait: float,
) -> Tuple[bool, str]:
    """Invio email con retry esponenziale. Ritorna (ok, last_error)."""
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
            time.sleep(base_wait * attempt)
    return False, last_err


# =============================================================================
# Helpers TicketOne
# =============================================================================

def _get_ticketone_mapping_for_performance(
    perf: Performance,
) -> Tuple[Optional[str], Optional[str], Optional[int]]:
    """
    Recupera l'URL TicketOne collegato alla performance.
    Cerca prima in PerformancePiattaforma, poi in EventoPiattaforma.
    Ritorna: (url, mapping_type, mapping_pk)
    """
    pp = (
        PerformancePiattaforma.objects
        .filter(performance=perf, piattaforma__nome__iexact="ticketone")
        .exclude(url="")
        .order_by("-aggiornato_il")
        .first()
    )
    if pp and pp.url:
        return pp.url, "performance", pp.pk

    ep = (
        EventoPiattaforma.objects
        .filter(evento=perf.evento, piattaforma__nome__iexact="ticketone")
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


def _was_scanned_recently(
    mapping_type: Optional[str],
    mapping_pk: Optional[int],
    hours: int = SKIP_IF_SCANNED_WITHIN_HOURS,
) -> bool:
    """PATCH 4: ritorna True se il mapping è stato scansionato nelle ultime N ore."""
    if not mapping_type or not mapping_pk:
        return False
    threshold = timezone.now() - timezone.timedelta(hours=hours)
    if mapping_type == "performance":
        obj = PerformancePiattaforma.objects.filter(pk=mapping_pk).values("ultima_scansione").first()
    elif mapping_type == "evento":
        obj = EventoPiattaforma.objects.filter(pk=mapping_pk).values("ultima_scansione").first()
    else:
        return False
    if not obj:
        return False
    ultima = obj.get("ultima_scansione")
    return bool(ultima and ultima > threshold)


def _ticketone_result_is_available(result: Dict[str, Any]) -> bool:
    """
    Segnale forte richiesto per dichiarare available un risultato TicketOne.
    Richiede min_price, raw_price_text, o detail_status=ok.
    """
    if result.get("min_price") is not None:
        return True
    if result.get("raw_price_text"):
        return True
    if result.get("detail_status") == "ok":
        return True
    return False


def _build_email_message(
    *,
    user_email: str,
    event_name: str,
    perf: Performance,
    url: str,
    price_data: Dict[str, Any],
) -> Tuple[str, str]:
    """
    Costruisce subject e body email.
    PATCH 1: rimosso [TEST Tixy] — subject sempre con [Tixy].
    PATCH 5: rimosso parametro forced.
    """
    luogo = perf.luogo.nome if perf.luogo else "Luogo non disponibile"
    data_evento = (
        perf.starts_at_utc.strftime("%d/%m/%Y %H:%M")
        if perf.starts_at_utc
        else "Data non disponibile"
    )

    min_price = price_data.get("min_price")
    currency = price_data.get("currency") or "EUR"
    raw_price_text = price_data.get("raw_price_text")
    detail_status = price_data.get("detail_status")
    source_used = price_data.get("source_used")

    # PATCH 1: sempre [Tixy], mai [TEST Tixy]
    subject = f"[Tixy] Biglietti disponibili su TicketOne - {event_name}"

    message = (
        f"Ciao,\n\n"
        f"abbiamo trovato un aggiornamento per il tuo monitoraggio PRO su TicketOne.\n\n"
        f"Evento: {event_name}\n"
        f"Luogo: {luogo}\n"
        f"Data: {data_evento}\n\n"
    )

    if min_price is not None:
        message += f"Prezzo rilevato: da {min_price} {currency}\n"
    elif raw_price_text:
        message += f"Prezzo rilevato: {raw_price_text}\n"
    else:
        message += "Prezzo: non disponibile o non rilevato\n"

    message += (
        f"\nStato controllo: {detail_status}\n"
        f"Fonte controllo: {source_used}\n"
        f"\nLink TicketOne:\n{url}\n\n"
        f"Grazie,\nTixy\n"
    )

    return subject, message


# =============================================================================
# Command
# =============================================================================

class Command(BaseCommand):
    help = "Scansiona monitoraggi PRO TicketOne e invia email quando trova disponibilità/prezzo."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=100,
                            help="Quanti monitoraggi massimo processare.")
        parser.add_argument("--sleep", type=float, default=1.0,
                            help="Pausa base tra controlli esterni.")
        parser.add_argument("--dry-run", action="store_true",
                            help="Non invia email e non salva Notifica.")
        parser.add_argument("--verbose", action="store_true",
                            help="Log dettagliato.")
        parser.add_argument("--no-browser", action="store_true",
                            help="Non usa fallback browser, solo HTTP.")
        parser.add_argument("--browser-headless", action="store_true",
                            help="Usa browser headless nel fallback.")
        parser.add_argument("--email-retries", type=int, default=3,
                            help="Tentativi invio email.")
        parser.add_argument("--email-wait", type=float, default=1.5,
                            help="Attesa base retry email.")
        # PATCH 2: --only-email per test controllati
        parser.add_argument("--only-email", type=str, default=None,
                            help="Processa solo i monitoraggi di questo utente. Per test.")
        # PATCH 4: --skip-scan-hours anti rate-limit
        parser.add_argument("--skip-scan-hours", type=int, default=SKIP_IF_SCANNED_WITHIN_HOURS,
                            help="Skip mapping scansionato nelle ultime N ore. 0=disabilitato.")
        # PATCH 3: --include-past per includere performance passate
        parser.add_argument("--include-past", action="store_true",
                            help="Includi performance già passate (default: skip).")

    def handle(self, *args, **opts):
        limit = int(opts["limit"])
        sleep_s = float(opts["sleep"])
        dry_run = bool(opts["dry_run"])
        verbose = bool(opts["verbose"])
        no_browser = bool(opts["no_browser"])
        browser_headless = bool(opts["browser_headless"])
        email_retries = max(1, int(opts["email_retries"]))
        email_wait = max(0.5, float(opts["email_wait"]))
        skip_scan_hours = max(0, int(opts.get("skip_scan_hours") or 0))
        include_past = bool(opts.get("include_past", False))

        only_email = (opts.get("only_email") or "").strip().lower() or None

        now = timezone.now()

        self.stdout.write(self.style.WARNING(
            f"[START] scan_ticketone_pro_alerts "
            f"limit={limit} dry_run={dry_run} skip_scan_hours={skip_scan_hours}"
        ))

        qs = (
            Monitoraggio.objects
            .filter(
                abbonamento__attivo=True,
                abbonamento__plan__plan_type="PRO",
                performance__isnull=False,
            )
            .filter(
                Q(abbonamento__data_fine__isnull=True) |
                Q(abbonamento__data_fine__gte=now)
            )
            .select_related(
                "abbonamento", "abbonamento__utente", "abbonamento__plan",
                "performance", "performance__evento", "performance__luogo",
            )
            .order_by("id")
        )

        if only_email:
            qs = qs.filter(abbonamento__utente__email__iexact=only_email)

        if verbose:
            self.stdout.write(f"[DEBUG] now={now.isoformat()} qs_count={qs.count()}")

        counters = {
            "processed": 0,
            "notified": 0,
            "skip_no_perf": 0,
            "skip_past": 0,           # PATCH 3
            "skip_inactive_abbonamento": 0,
            "skip_internal": 0,
            "skip_no_mapping": 0,
            "skip_rate_limit": 0,     # PATCH 4
            "skip_not_available": 0,
            "skip_dedup": 0,
            "ticketone_error": 0,
            "email_fail": 0,
            "no_email_pref": 0,
        }

        # Pre-filtra: solo monitoraggi con mapping TicketOne valido
        monitoraggi = []

        for m in qs[: limit * 10]:
            try:
                if not _abbonamento_is_active(m.abbonamento):
                    counters["skip_inactive_abbonamento"] += 1
                    continue

                perf = m.performance
                if perf is None:
                    counters["skip_no_perf"] += 1
                    continue

                # PATCH 3: skip performance passate
                if not include_past and _performance_is_past(perf):
                    counters["skip_past"] += 1
                    continue

                url, mapping_type, mapping_pk = _get_ticketone_mapping_for_performance(perf)
                if not url:
                    counters["skip_no_mapping"] += 1
                    continue

                monitoraggi.append(m)

            except Exception as exc:
                if verbose:
                    self.stdout.write(self.style.ERROR(
                        f"[PRE-FILTER ERROR] monitoraggio={m.id} error={exc}"
                    ))
                continue

            if len(monitoraggi) >= limit:
                break

        self.stdout.write(
            f"[SCAN] monitoraggi PRO TicketOne attivi: {len(monitoraggi)} (limit={limit})"
        )

        for m in monitoraggi:
            counters["processed"] += 1

            try:
                user = m.abbonamento.utente
                perf = m.performance

                if perf is None:
                    counters["skip_no_perf"] += 1
                    continue

                if _has_internal_tickets(perf):
                    counters["skip_internal"] += 1
                    if verbose:
                        self.stdout.write(f"[SKIP INTERNAL] monitoraggio={m.id} perf={perf.id}")
                    continue

                url, mapping_type, mapping_pk = _get_ticketone_mapping_for_performance(perf)
                if not url:
                    counters["skip_no_mapping"] += 1
                    continue

                # PATCH 4: anti rate-limit
                if skip_scan_hours > 0 and _was_scanned_recently(mapping_type, mapping_pk, skip_scan_hours):
                    counters["skip_rate_limit"] += 1
                    if verbose:
                        self.stdout.write(
                            f"[SKIP RATE] monitoraggio={m.id} perf={perf.id}: "
                            f"già scansionato nelle ultime {skip_scan_hours}h"
                        )
                    continue

                event_name = perf.evento.nome_evento if perf.evento else f"Performance {perf.id}"

                if verbose:
                    self.stdout.write(
                        f"[CHECK] monitoraggio={m.id} perf={perf.id} "
                        f"evento={event_name} url={url}"
                    )

                try:
                    price_data = get_ticketone_price_data(
                        url,
                        verbose=verbose,
                        use_browser_fallback=not no_browser,
                        browser_headless=browser_headless,
                    )
                    _touch_last_scan(mapping_type, mapping_pk)

                except Exception as exc:
                    counters["ticketone_error"] += 1
                    if verbose:
                        self.stdout.write(self.style.ERROR(
                            f"[TICKETONE ERROR] perf={perf.id} error={exc}"
                        ))
                    continue

                is_available = _ticketone_result_is_available(price_data)

                if not is_available:
                    counters["skip_not_available"] += 1
                    if verbose:
                        self.stdout.write(
                            f"[SKIP NOT AVAILABLE] perf={perf.id} "
                            f"status={price_data.get('detail_status')} "
                            f"source={price_data.get('source_used')}"
                        )
                    time.sleep(sleep_s)
                    continue

                dedupe = _dedupe_key(perf.id, user.id, "ticketone", "AVAILABLE")

                if Notifica.objects.filter(dedupe_key=dedupe, status="SENT").exists():
                    counters["skip_dedup"] += 1
                    if verbose:
                        self.stdout.write(f"[DEDUP] perf={perf.id} già notificata oggi")
                    time.sleep(sleep_s)
                    continue

                if not getattr(user, "notify_email", True):
                    counters["no_email_pref"] += 1
                    if verbose:
                        self.stdout.write(f"[SKIP EMAIL PREF] user={user.id}")
                    time.sleep(sleep_s)
                    continue

                to_email = getattr(user, "email", None)
                if not to_email:
                    counters["no_email_pref"] += 1
                    if verbose:
                        self.stdout.write(f"[SKIP NO EMAIL] user={user.id}")
                    time.sleep(sleep_s)
                    continue

                subject, message = _build_email_message(
                    user_email=to_email,
                    event_name=event_name,
                    perf=perf,
                    url=url,
                    price_data=price_data,
                )

                if dry_run:
                    self.stdout.write(self.style.WARNING(
                        f"[DRY-RUN EMAIL] to={to_email} subject={subject} dedupe={dedupe}"
                    ))
                    counters["notified"] += 1
                    time.sleep(sleep_s)
                    continue

                ok, err = _send_email_with_retry(
                    subject=subject,
                    message=message,
                    to_email=to_email,
                    max_retries=email_retries,
                    base_wait=email_wait,
                )

                with transaction.atomic():
                    min_price = _to_decimal(price_data.get("min_price"))
                    if min_price is not None:
                        perf.prezzo_min = min_price
                        perf.valuta = price_data.get("currency") or "EUR"
                        perf.disponibilita_agg = "disponibile"
                        perf.save(update_fields=["prezzo_min", "valuta", "disponibilita_agg"])

                    if ok:
                        Notifica.objects.create(
                            monitoraggio=m,
                            channel="email",
                            dedupe_key=dedupe,
                            status="SENT",
                            sent_at=timezone.now(),
                            message=message,
                        )
                        counters["notified"] += 1
                        self.stdout.write(self.style.SUCCESS(
                            f"[EMAIL SENT] monitoraggio={m.id} perf={perf.id} to={to_email}"
                        ))
                    else:
                        Notifica.objects.create(
                            monitoraggio=m,
                            channel="email",
                            dedupe_key=dedupe,
                            status="FAILED",
                            message=f"{message}\n\nERRORE INVIO EMAIL:\n{err}",
                        )
                        counters["email_fail"] += 1
                        self.stdout.write(self.style.ERROR(
                            f"[EMAIL FAIL] monitoraggio={m.id} perf={perf.id} "
                            f"to={to_email} err={err}"
                        ))

                time.sleep(sleep_s)

            except Exception as exc:
                counters["ticketone_error"] += 1
                self.stdout.write(self.style.ERROR(
                    f"[ERROR] monitoraggio={m.id} error={exc}"
                ))
                continue

        # PATCH 6: riepilogo finale esteso
        self.stdout.write(self.style.SUCCESS(
            "[DONE] "
            f"processed={counters['processed']} "
            f"notified={counters['notified']} "
            f"skip_no_perf={counters['skip_no_perf']} "
            f"skip_past={counters['skip_past']} "
            f"skip_inactive={counters['skip_inactive_abbonamento']} "
            f"skip_internal={counters['skip_internal']} "
            f"skip_no_mapping={counters['skip_no_mapping']} "
            f"skip_rate_limit={counters['skip_rate_limit']} "
            f"skip_not_available={counters['skip_not_available']} "
            f"skip_dedup={counters['skip_dedup']} "
            f"ticketone_error={counters['ticketone_error']} "
            f"email_fail={counters['email_fail']} "
            f"no_email_pref={counters['no_email_pref']}"
        ))