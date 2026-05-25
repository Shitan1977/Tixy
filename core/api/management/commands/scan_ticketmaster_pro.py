from __future__ import annotations

import random
import time
from typing import Optional, Tuple

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

from api.scrapers.ticketmaster_availability import check_ticketmaster_mapping_availability


# -------------------------
# Costanti di sicurezza
# -------------------------

# PATCH 4 — Reason "available" che non triggherano email.
# Sono segnali troppo deboli o provenienti da fonti inaffidabili.
# Aggiunti qui i reason legacy (prima della patch) che potrebbero
# ancora apparire se il file scraper non fosse aggiornato.
_WEAK_AVAILABLE_REASONS: frozenset[str] = frozenset({
    # HTML statico — frasi rimosse da strong_positive ma ancora
    # possibili se lo scraper non è stato aggiornato
    "strong_positive_keyword:buy tickets",
    "strong_positive_keyword:acquista biglietti",
    "strong_positive_keyword:tickets available",
    "strong_positive_keyword:find tickets",
    "strong_positive_keyword:get tickets",
    "strong_positive_keyword:select tickets",
    # Discovery senza conferma reale
    "discovery_available_url_match",
})


def _is_reliable_available(availability: str, reason: str) -> bool:
    """
    Ritorna True solo se availability è "available" e la reason contiene
    un segnale esplicitamente forte.

    Regola prudente:
    meglio perdere un alert vero che inviare un falso positivo.
    """
    if availability != "available":
        return False

    reason = (reason or "").lower().strip()

    if not reason:
        return False

    weak_tokens = [
        "buy tickets",
        "acquista biglietti",
        "tickets available",
        "find tickets",
        "get tickets",
        "select tickets",
        "discovery_available_url_match",
        "weak_positive_keyword",
        "price_without_context",
        "no_strong_signals",
    ]

    for token in weak_tokens:
        if token in reason:
            return False

    strong_tokens = [
        "browser_price_detected",
        "strong_positive_keyword:aggiungi al carrello",
        "strong_positive_keyword:procedi all'acquisto",
        "strong_positive_keyword:procedi con l'acquisto",
        "strong_positive_keyword:seleziona biglietti",
        "strong_positive_keyword:scegli i biglietti",
    ]

    for token in strong_tokens:
        if token in reason:
            return True

    return False

# -------------------------
# Helpers abbonamenti / DB
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


def _extract_tm_code_from_url(url: Optional[str]) -> Optional[str]:
    if not url:
        return None

    clean_url = str(url).strip().rstrip("/")

    if not clean_url:
        return None

    if "/event/" in clean_url:
        return clean_url.split("/event/")[-1].split("?")[0].split("#")[0].strip()

    return clean_url.split("/")[-1].split("?")[0].split("#")[0].strip()


def _get_ticketmaster_mapping_for_performance(
    perf: Performance,
) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[int]]:
    pp = (
        PerformancePiattaforma.objects
        .filter(performance=perf, piattaforma__nome__iexact="ticketmaster")
        .exclude(url="")
        .order_by("-aggiornato_il")
        .first()
    )

    if pp and pp.url:
        tm_id = getattr(pp, "id_evento_piattaforma", None) or _extract_tm_code_from_url(pp.url)

        return pp.url, tm_id, "performance", pp.pk

    ep = (
        EventoPiattaforma.objects
        .filter(evento=perf.evento, piattaforma__nome__iexact="ticketmaster")
        .exclude(url="")
        .order_by("-aggiornato_il")
        .first()
    )

    if ep and ep.url:
        tm_id = getattr(ep, "id_evento_piattaforma", None) or _extract_tm_code_from_url(ep.url)

        return ep.url, tm_id, "evento", ep.pk

    return None, None, None, None


def _touch_last_scan(mapping_type: Optional[str], mapping_pk: Optional[int]) -> None:
    if not mapping_type or not mapping_pk:
        return

    now = timezone.now()

    if mapping_type == "performance":
        PerformancePiattaforma.objects.filter(pk=mapping_pk).update(ultima_scansione=now)

    elif mapping_type == "evento":
        EventoPiattaforma.objects.filter(pk=mapping_pk).update(ultima_scansione=now)


def _sleep_with_jitter(base: float, *, heavy: bool = False) -> None:
    jitter = random.uniform(0.15, 0.85)
    extra = random.uniform(2.0, 6.0) if heavy else 0.0

    time.sleep(max(0.0, base + jitter + extra))


def _send_email_with_retry(
    *,
    subject: str,
    message: str,
    to_email: str,
    max_retries: int,
    base_wait: float,
) -> Tuple[bool, str]:
    last_error = ""

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

        except Exception as ex:
            last_error = str(ex)
            wait = base_wait * attempt
            time.sleep(wait)

    return False, last_error


def _format_when(perf: Performance) -> str:
    starts_at = getattr(perf, "starts_at_utc", None)

    if not starts_at:
        return "—"

    try:
        return timezone.localtime(starts_at).strftime("%d/%m/%Y %H:%M")
    except Exception:
        return starts_at.isoformat()


def _build_email_message(
    *,
    user,
    perf: Performance,
    tm_url: str,
    result: dict,
) -> Tuple[str, str]:
    event_title = perf.evento.nome_evento if getattr(perf, "evento_id", None) else "Evento"
    luogo = getattr(perf.luogo, "nome", "") if getattr(perf, "luogo_id", None) else ""
    when = _format_when(perf)

    price = result.get("price")
    price_line = f"- Prezzo rilevato: {price}\n" if price else ""

    subject = f"Biglietti disponibili: {event_title}"

    message = (
        f"Ciao {getattr(user, 'first_name', '')},\n\n"
        f"Sono tornati disponibili biglietti su Ticketmaster per:\n"
        f"- Evento: {event_title}\n"
        f"- Luogo: {luogo}\n"
        f"- Data: {when}\n"
        f"{price_line}\n"
        f"Link: {tm_url}\n\n"
        f"— Tixy"
    )

    return subject, message


# -------------------------
# Command
# -------------------------

class Command(BaseCommand):
    help = "Scansiona monitoraggi PRO attivi e invia email quando Ticketmaster torna disponibile."

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit",
            type=int,
            default=200,
            help="Quanti monitoraggi massimo processare.",
        )

        parser.add_argument(
            "--sleep",
            type=float,
            default=0.35,
            help="Pausa base tra controlli esterni.",
        )

        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Non invia email e non salva Notifica.",
        )

        parser.add_argument(
            "--verbose",
            action="store_true",
            help="Log più dettagliato.",
        )

        parser.add_argument(
            "--email-retries",
            type=int,
            default=3,
            help="Quanti tentativi per inviare email.",
        )

        parser.add_argument(
            "--email-wait",
            type=float,
            default=1.5,
            help="Attesa base tra tentativi email.",
        )

        parser.add_argument(
            "--only-email",
            type=str,
            default=None,
            help="Processa solo i monitoraggi dell'utente con questa email. Utile per test controllati.",
        )

    def handle(self, *args, **opts):
        limit = int(opts["limit"])
        sleep_s = float(opts["sleep"])
        dry_run = bool(opts["dry_run"])
        verbose = bool(opts["verbose"])
        email_retries = max(1, int(opts["email_retries"]))
        email_wait = max(0.5, float(opts["email_wait"]))

        only_email = opts.get("only_email")

        if only_email:
            only_email = only_email.strip().lower()

        now = timezone.now()

        qs = (
            Monitoraggio.objects
            .filter(
                abbonamento__attivo=True,
                abbonamento__prezzo__gt=0,
            )
            .filter(
                Q(abbonamento__data_fine__isnull=True)
                | Q(abbonamento__data_fine__gte=now)
            )
        )

        if only_email:
            qs = qs.filter(abbonamento__utente__email__iexact=only_email)

        qs = (
            qs
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

        monitoraggi = []

        for monitoraggio in qs[: limit * 5]:
            try:
                if _abbonamento_is_active(monitoraggio.abbonamento):
                    monitoraggi.append(monitoraggio)
            except Exception:
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
            "skip_weak_signal": 0,   # PATCH 4 — nuovo contatore
            "skip_dedup": 0,
            "tm_error": 0,
            "email_fail": 0,
            "no_email_pref": 0,
        }

        for monitoraggio in monitoraggi:
            counters["processed"] += 1

            try:
                user = monitoraggio.abbonamento.utente
                perf = monitoraggio.performance

                if perf is None:
                    counters["skip_no_perf"] += 1

                    if verbose:
                        self.stdout.write(
                            f"[SKIP] monitoraggio {monitoraggio.id}: performance mancante"
                        )

                    continue

                if _has_internal_tickets(perf):
                    counters["skip_internal"] += 1

                    if verbose:
                        self.stdout.write(
                            f"[SKIP] perf {perf.id}: biglietti già presenti (DB/listing)"
                        )

                    continue

                tm_url, tm_id, mapping_type, mapping_pk = _get_ticketmaster_mapping_for_performance(perf)

                if not tm_url or not tm_id:
                    counters["skip_no_mapping"] += 1

                    if verbose:
                        self.stdout.write(f"[SKIP] perf {perf.id}: mapping TM assente")

                    continue

                try:
                    result = check_ticketmaster_mapping_availability(
                        tm_id=tm_id,
                        url=tm_url,
                    )

                except Exception as ex:
                    counters["tm_error"] += 1

                    if verbose:
                        self.stdout.write(
                            f"[TM EXC] perf {perf.id} tm_id={tm_id} url={tm_url} err={ex}"
                        )

                    _touch_last_scan(mapping_type, mapping_pk)
                    _sleep_with_jitter(sleep_s, heavy=True)
                    continue

                _touch_last_scan(mapping_type, mapping_pk)

                if not result.get("ok"):
                    counters["tm_error"] += 1

                    status_code = result.get("status_code")

                    if verbose:
                        self.stdout.write(
                            f"[TM ERR] perf {perf.id} tm_id={tm_id} "
                            f"status={status_code} reason={result.get('reason')}"
                        )

                    if status_code in (403, 429):
                        _sleep_with_jitter(sleep_s, heavy=True)
                    else:
                        _sleep_with_jitter(sleep_s)

                    continue

                availability = result.get("availability")
                reason = result.get("reason", "")

                if availability != "available":
                    counters["skip_not_avail"] += 1

                    if verbose:
                        self.stdout.write(
                            f"[TM] perf {perf.id} tm_id={tm_id} => {availability} "
                            f"({reason})"
                        )

                    _sleep_with_jitter(sleep_s)
                    continue

                # PATCH 4 — Doppio controllo: "available" non basta.
                # Verifica che il segnale provenga da una fonte affidabile.
                if not _is_reliable_available(availability, reason):
                    counters["skip_weak_signal"] += 1

                    self.stdout.write(
                        f"[WEAK SIGNAL] perf {perf.id} tm_id={tm_id} "
                        f"availability={availability} reason={reason} => skip email"
                    )

                    _sleep_with_jitter(sleep_s)
                    continue

                if verbose:
                    self.stdout.write(
                        f"[TM AVAILABLE] perf {perf.id} tm_id={tm_id} "
                        f"price={result.get('price')} reason={reason}"
                    )

                dedupe_key = _dedupe_key(
                    perf.id,
                    user.id,
                    "ticketmaster",
                    "BACK_IN_STOCK",
                )

                if Notifica.objects.filter(dedupe_key=dedupe_key, status="SENT").exists():
                    counters["skip_dedup"] += 1

                    if verbose:
                        self.stdout.write(
                            f"[DEDUP] perf {perf.id} già notificata oggi (SENT)"
                        )

                    _sleep_with_jitter(sleep_s)
                    continue

                if not getattr(user, "notify_email", True):
                    counters["no_email_pref"] += 1

                    if verbose:
                        self.stdout.write(
                            f"[NO EMAIL PREF] user={getattr(user, 'email', None)}"
                        )

                    _sleep_with_jitter(sleep_s)
                    continue

                subject, message = _build_email_message(
                    user=user,
                    perf=perf,
                    tm_url=tm_url,
                    result=result,
                )

                if dry_run:
                    self.stdout.write(
                        f"[DRY] WOULD EMAIL user={user.email} "
                        f"perf={perf.id} url={tm_url} price={result.get('price')} reason={reason}"
                    )

                    counters["notified"] += 1
                    _sleep_with_jitter(sleep_s)
                    continue

                ok, error = _send_email_with_retry(
                    subject=subject,
                    message=message,
                    to_email=user.email,
                    max_retries=email_retries,
                    base_wait=email_wait,
                )

                if not ok:
                    counters["email_fail"] += 1

                    self.stdout.write(
                        f"[EMAIL FAIL] {user.email} perf={perf.id} last_err={error}"
                    )

                    _sleep_with_jitter(sleep_s)
                    continue

                with transaction.atomic():
                    Notifica.objects.create(
                        monitoraggio=monitoraggio,
                        channel="email",
                        dedupe_key=dedupe_key,
                        status="SENT",
                        sent_at=timezone.now(),
                        message=message,
                    )

                counters["notified"] += 1

                self.stdout.write(f"[EMAIL OK] {user.email} perf={perf.id} reason={reason}")

                _sleep_with_jitter(sleep_s)

            except Exception as ex:
                self.stdout.write(
                    f"[FATAL-SKIP] monitoraggio={getattr(monitoraggio, 'id', None)} err={ex}"
                )

                _sleep_with_jitter(sleep_s, heavy=True)
                continue

        self.stdout.write(
            "[DONE] "
            f"processed={counters['processed']} "
            f"notified={counters['notified']} "
            f"skip_no_perf={counters['skip_no_perf']} "
            f"skip_internal={counters['skip_internal']} "
            f"skip_no_mapping={counters['skip_no_mapping']} "
            f"skip_not_avail={counters['skip_not_avail']} "
            f"skip_weak_signal={counters['skip_weak_signal']} "
            f"skip_dedup={counters['skip_dedup']} "
            f"tm_error={counters['tm_error']} "
            f"email_fail={counters['email_fail']} "
            f"no_email_pref={counters['no_email_pref']}"
        )