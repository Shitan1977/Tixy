from __future__ import annotations

# =============================================================================
# scan_ticketmaster_pro.py  — VERSIONE DEFINITIVA
# =============================================================================
# CHANGELOG:
#   PATCH 1 — filtro abbonamenti scaduti con attivo=True
#   PATCH 2 — _is_reliable_available: strong_tokens PRIMA di weak_tokens
#   PATCH 3 — _extract_tm_code_from_url: gestione URL partner shop.ticketmaster.it
#   PATCH 4 — anti rate-limit Discovery API: skip se già scansionato nelle ultime N ore
#   PATCH 5 — skip monitoraggi con performance passate (data evento già trascorsa)
#   PATCH 6 — log finale con contatori estesi
# =============================================================================

import random
import re
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


# =============================================================================
# CONFIGURAZIONE ANTI RATE-LIMIT
# =============================================================================
#
# La Discovery API Ticketmaster ha un limite di ~5.000 chiamate/giorno per key.
# Con 1993 mapping TM e cron ogni 5 minuti faremmo 573.000+ chiamate/giorno.
#
# Strategia:
#   1. skip_if_scanned_within_hours: non riscansionare un mapping scansionato
#      di recente (default 4 ore). Riduce drasticamente le chiamate API.
#   2. Il browser Playwright viene usato solo se l'HTML statico non è conclusivo.
#   3. La Discovery API viene chiamata solo se HTML + browser non bastano.
#
# Con skip 4 ore: ogni mapping viene scansionato max 6 volte/giorno
# → 1993 * 6 = ~12.000 operazioni/giorno, ma solo i monitoraggi PRO attivi
# vengono controllati (tipicamente 10-50), non tutti i mapping TM nel DB.
#
SKIP_IF_SCANNED_WITHIN_HOURS: int = 4


# =============================================================================
# Segnali affidabili di disponibilità
# =============================================================================

def _is_reliable_available(availability: str, reason: str) -> bool:
    """
    Ritorna True SOLO se availability è "available" E la reason contiene
    un segnale forte proveniente da browser o scraper affidabile.

    PATCH 2: i strong_tokens vengono controllati PRIMA dei weak_tokens.
    Questo risolve il bug per cui browser_price_detected veniva bloccato
    dalla presenza di weak_positive_keyword nella stessa reason composta.

    Logica:
    - Se il browser ha trovato un prezzo reale nel DOM → True (segnale forte)
    - Se HTML statico ha trovato solo keyword generiche → False (segnale debole)
    - Se nessun segnale forte è presente → False (prudenza)
    """
    if availability != "available":
        return False

    reason_lower = (reason or "").lower().strip()

    if not reason_lower:
        return False

    # ── Segnali FORTI: controllati per primi ─────────────────────────────────
    # Se almeno uno è presente nella reason → disponibilità confermata.
    strong_tokens = [
        "browser_price_detected",           # Browser ha trovato prezzo nel DOM
        "strong_positive_keyword:aggiungi al carrello",
        "strong_positive_keyword:procedi all'acquisto",
        "strong_positive_keyword:procedi con l'acquisto",
        "strong_positive_keyword:seleziona biglietti",
        "strong_positive_keyword:scegli i biglietti",
        "resale_price_strong_signal",       # Resale con prezzo confermato
        "html+prices",                      # Merge HTML + API prezzi
    ]

    for token in strong_tokens:
        if token in reason_lower:
            return True

    # ── Segnali DEBOLI: se presenti → scarta ─────────────────────────────────
    # Questi indicano che la pagina ha keyword generiche ma non biglietti reali.
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
        if token in reason_lower:
            return False

    # Nessun segnale forte trovato → prudenza
    return False


# =============================================================================
# Helpers abbonamenti / DB
# =============================================================================

def _abbonamento_is_active(ab: Abbonamento) -> bool:
    """
    Verifica che l'abbonamento sia attivo E non scaduto.

    PATCH 1: controlla data_fine anche se attivo=True, perché nel DB
    esistono abbonamenti con attivo=True ma data_fine nel passato
    (bug di consistenza da correggere separatamente con un management
    command di pulizia).
    """
    if not getattr(ab, "attivo", False):
        return False

    data_fine = getattr(ab, "data_fine", None)
    if data_fine and data_fine < timezone.now():
        return False

    return True


def _has_internal_tickets(perf: Performance) -> bool:
    """Controlla se esistono già biglietti interni o listing attivi per questa performance."""
    if Biglietto.objects.filter(performance=perf, is_valid=True).exists():
        return True
    if Listing.objects.filter(performance=perf, status="ACTIVE").exists():
        return True
    return False


def _performance_is_past(perf: Performance) -> bool:
    """
    PATCH 5: salta performance già passate.
    Non ha senso controllare Ticketmaster per eventi già avvenuti.
    """
    starts_at = getattr(perf, "starts_at_utc", None)
    if starts_at and starts_at < timezone.now():
        return True
    return False


def _was_scanned_recently(
    mapping_type: Optional[str],
    mapping_pk: Optional[int],
    hours: int = SKIP_IF_SCANNED_WITHIN_HOURS,
) -> bool:
    """
    PATCH 4: anti rate-limit.
    Ritorna True se il mapping è stato scansionato nelle ultime `hours` ore.
    In quel caso lo scanner salta la chiamata esterna (HTML + API + browser).

    Legge il campo ultima_scansione da PerformancePiattaforma o EventoPiattaforma.
    Se il campo non esiste o è None → non skippa (prima scansione).
    """
    if not mapping_type or not mapping_pk:
        return False

    now = timezone.now()
    threshold = now - timezone.timedelta(hours=hours)

    if mapping_type == "performance":
        obj = PerformancePiattaforma.objects.filter(pk=mapping_pk).values("ultima_scansione").first()
    elif mapping_type == "evento":
        obj = EventoPiattaforma.objects.filter(pk=mapping_pk).values("ultima_scansione").first()
    else:
        return False

    if not obj:
        return False

    ultima = obj.get("ultima_scansione")
    if ultima and ultima > threshold:
        return True

    return False


def _dedupe_key(perf_id: int, user_id: int, platform: str, reason: str) -> str:
    """Chiave giornaliera per deduplicare notifiche: una email per utente/perf/giorno."""
    day = timezone.now().date().isoformat()
    return f"{platform}:{reason}:perf:{perf_id}:user:{user_id}:{day}"


def _extract_tm_code_from_url(url: Optional[str]) -> Optional[str]:
    """
    Estrae il codice evento Ticketmaster dall'URL.

    PATCH 3: gestisce correttamente gli URL del widget partner:
      shop.ticketmaster.it/partner/biglietti/nome-evento-15471.html
    → estrae solo il numero finale (es. "15471"), non l'intero slug.

    Casi gestiti:
      - /event/CODICE          → CODICE (URL standard IT e .com)
      - /partner/.../N.html   → N (URL white-label partner)
      - fallback               → ultimo segmento del path
    """
    if not url:
        return None

    clean_url = str(url).strip().rstrip("/")
    if not clean_url:
        return None

    # URL partner: shop.ticketmaster.it/partner/biglietti/slug-15471.html
    if "/partner/" in clean_url:
        m = re.search(r"-(\d{4,6})\.html$", clean_url)
        if m:
            return m.group(1)
        # Fallback: prendi tutto dopo l'ultimo /
        return clean_url.split("/")[-1].split("?")[0].split("#")[0].strip()

    # URL standard: .../event/CODICE
    if "/event/" in clean_url:
        return clean_url.split("/event/")[-1].split("?")[0].split("#")[0].strip()

    # Fallback generico
    return clean_url.split("/")[-1].split("?")[0].split("#")[0].strip()


def _get_ticketmaster_mapping_for_performance(
    perf: Performance,
) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[int]]:
    """
    Cerca il mapping Ticketmaster per la performance.

    Priorità:
    1. PerformancePiattaforma (mapping specifico della performance)
    2. EventoPiattaforma (mapping dell'evento padre)

    Ritorna: (url, tm_id, mapping_type, mapping_pk)
    """
    # 1. Cerca mapping sulla performance
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

    # 2. Fallback: cerca mapping sull'evento
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
    """Aggiorna ultima_scansione sul mapping dopo ogni controllo esterno."""
    if not mapping_type or not mapping_pk:
        return

    now = timezone.now()

    if mapping_type == "performance":
        PerformancePiattaforma.objects.filter(pk=mapping_pk).update(ultima_scansione=now)
    elif mapping_type == "evento":
        EventoPiattaforma.objects.filter(pk=mapping_pk).update(ultima_scansione=now)


def _sleep_with_jitter(base: float, *, heavy: bool = False) -> None:
    """Pausa con jitter random per non essere identificati come bot."""
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
    """Invia email con retry esponenziale. Ritorna (ok, last_error)."""
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
            time.sleep(base_wait * attempt)

    return False, last_error


def _format_when(perf: Performance) -> str:
    """Formatta la data della performance in italiano."""
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
    """Costruisce subject e body dell'email di alert."""
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


# =============================================================================
# Command
# =============================================================================

class Command(BaseCommand):
    help = (
        "Scansiona monitoraggi PRO attivi e invia email quando Ticketmaster "
        "torna disponibile. Include anti-rate-limit, fix URL partner, "
        "filtro segnali deboli e deduplica giornaliera."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit",
            type=int,
            default=200,
            help="Quanti monitoraggi massimo processare per run.",
        )
        parser.add_argument(
            "--sleep",
            type=float,
            default=0.35,
            help="Pausa base (secondi) tra controlli esterni.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Non invia email e non salva Notifica. Solo log.",
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
            help="Tentativi massimi per inviare ogni email.",
        )
        parser.add_argument(
            "--email-wait",
            type=float,
            default=1.5,
            help="Attesa base (secondi) tra tentativi email.",
        )
        parser.add_argument(
            "--only-email",
            type=str,
            default=None,
            help="Processa solo i monitoraggi di questo utente (email). Per test.",
        )
        parser.add_argument(
            "--skip-scan-hours",
            type=int,
            default=SKIP_IF_SCANNED_WITHIN_HOURS,
            help=(
                "Salta mapping già scansionati nelle ultime N ore "
                "(anti rate-limit Discovery API). Default: 4. "
                "Usa 0 per disabilitare."
            ),
        )
        parser.add_argument(
            "--include-past",
            action="store_true",
            help="Includi performance già passate (default: skip).",
        )

    def handle(self, *args, **opts):
        limit = int(opts["limit"])
        sleep_s = float(opts["sleep"])
        dry_run = bool(opts["dry_run"])
        verbose = bool(opts["verbose"])
        email_retries = max(1, int(opts["email_retries"]))
        email_wait = max(0.5, float(opts["email_wait"]))
        skip_scan_hours = max(0, int(opts["skip_scan_hours"]))
        include_past = bool(opts["include_past"])

        only_email = opts.get("only_email")
        if only_email:
            only_email = only_email.strip().lower()

        now = timezone.now()

        # ── Query base monitoraggi PRO attivi ────────────────────────────────
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
            if skip_scan_hours > 0:
                self.stdout.write(
                    f"[DEBUG] anti-rate-limit attivo: skip se scansionato "
                    f"nelle ultime {skip_scan_hours}h"
                )

        # ── Filtra per abbonamento davvero attivo (PATCH 1) ──────────────────
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
            "skip_past": 0,         # PATCH 5
            "skip_internal": 0,
            "skip_no_mapping": 0,
            "skip_rate_limit": 0,   # PATCH 4
            "skip_not_avail": 0,
            "skip_weak_signal": 0,
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

                # ── Skip: performance mancante ────────────────────────────
                if perf is None:
                    counters["skip_no_perf"] += 1
                    if verbose:
                        self.stdout.write(
                            f"[SKIP] monitoraggio {monitoraggio.id}: performance mancante"
                        )
                    continue

                # ── PATCH 5: skip performance già passate ─────────────────
                if not include_past and _performance_is_past(perf):
                    counters["skip_past"] += 1
                    if verbose:
                        self.stdout.write(
                            f"[SKIP PAST] perf {perf.id}: evento già passato "
                            f"({getattr(perf, 'starts_at_utc', '?')})"
                        )
                    continue

                # ── Skip: biglietti già presenti nel DB ───────────────────
                if _has_internal_tickets(perf):
                    counters["skip_internal"] += 1
                    if verbose:
                        self.stdout.write(
                            f"[SKIP] perf {perf.id}: biglietti già presenti (DB/listing)"
                        )
                    continue

                # ── Cerca mapping Ticketmaster ─────────────────────────────
                tm_url, tm_id, mapping_type, mapping_pk = _get_ticketmaster_mapping_for_performance(perf)

                if not tm_url or not tm_id:
                    counters["skip_no_mapping"] += 1
                    if verbose:
                        self.stdout.write(f"[SKIP] perf {perf.id}: mapping TM assente")
                    continue

                # ── PATCH 4: anti rate-limit — skip se scansionato di recente
                if skip_scan_hours > 0 and _was_scanned_recently(mapping_type, mapping_pk, skip_scan_hours):
                    counters["skip_rate_limit"] += 1
                    if verbose:
                        self.stdout.write(
                            f"[SKIP RATE] perf {perf.id} tm_id={tm_id}: "
                            f"già scansionato nelle ultime {skip_scan_hours}h"
                        )
                    continue

                # ── Chiama lo scraper Ticketmaster ────────────────────────
                try:
                    result = check_ticketmaster_mapping_availability(
                        tm_id=tm_id,
                        url=tm_url,
                    )
                except Exception as ex:
                    counters["tm_error"] += 1
                    if verbose:
                        self.stdout.write(
                            f"[TM EXC] perf {perf.id} tm_id={tm_id} err={ex}"
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
                    _sleep_with_jitter(sleep_s, heavy=(status_code in (403, 429)))
                    continue

                availability = result.get("availability")
                reason = result.get("reason", "")

                # ── Non disponibile o unknown → skip ──────────────────────
                if availability != "available":
                    counters["skip_not_avail"] += 1
                    if verbose:
                        self.stdout.write(
                            f"[TM] perf {perf.id} tm_id={tm_id} => {availability} ({reason})"
                        )
                    _sleep_with_jitter(sleep_s)
                    continue

                # ── PATCH 2: verifica segnale affidabile ──────────────────
                # strong_tokens prima di weak_tokens (fix bug blocco browser_price_detected)
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

                # ── Deduplica: una email per utente/perf/giorno ───────────
                dedupe_key = _dedupe_key(perf.id, user.id, "ticketmaster", "BACK_IN_STOCK")

                if Notifica.objects.filter(dedupe_key=dedupe_key, status="SENT").exists():
                    counters["skip_dedup"] += 1
                    if verbose:
                        self.stdout.write(
                            f"[DEDUP] perf {perf.id} già notificata oggi (SENT)"
                        )
                    _sleep_with_jitter(sleep_s)
                    continue

                # ── Preferenze email utente ───────────────────────────────
                if not getattr(user, "notify_email", True):
                    counters["no_email_pref"] += 1
                    if verbose:
                        self.stdout.write(
                            f"[NO EMAIL PREF] user={getattr(user, 'email', None)}"
                        )
                    _sleep_with_jitter(sleep_s)
                    continue

                # ── Costruisci email ──────────────────────────────────────
                subject, message = _build_email_message(
                    user=user,
                    perf=perf,
                    tm_url=tm_url,
                    result=result,
                )

                # ── Dry-run: solo log ─────────────────────────────────────
                if dry_run:
                    self.stdout.write(
                        f"[DRY] WOULD EMAIL user={user.email} "
                        f"perf={perf.id} url={tm_url} "
                        f"price={result.get('price')} reason={reason}"
                    )
                    counters["notified"] += 1
                    _sleep_with_jitter(sleep_s)
                    continue

                # ── Invia email reale ─────────────────────────────────────
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

                # ── Salva Notifica nel DB ─────────────────────────────────
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
                self.stdout.write(
                    f"[EMAIL OK] {user.email} perf={perf.id} reason={reason}"
                )
                _sleep_with_jitter(sleep_s)

            except Exception as ex:
                self.stdout.write(
                    f"[FATAL-SKIP] monitoraggio={getattr(monitoraggio, 'id', None)} err={ex}"
                )
                _sleep_with_jitter(sleep_s, heavy=True)
                continue

        # ── Riepilogo finale PATCH 6 ──────────────────────────────────────────
        self.stdout.write(
            "[DONE] "
            f"processed={counters['processed']} "
            f"notified={counters['notified']} "
            f"skip_no_perf={counters['skip_no_perf']} "
            f"skip_past={counters['skip_past']} "
            f"skip_internal={counters['skip_internal']} "
            f"skip_no_mapping={counters['skip_no_mapping']} "
            f"skip_rate_limit={counters['skip_rate_limit']} "
            f"skip_not_avail={counters['skip_not_avail']} "
            f"skip_weak_signal={counters['skip_weak_signal']} "
            f"skip_dedup={counters['skip_dedup']} "
            f"tm_error={counters['tm_error']} "
            f"email_fail={counters['email_fail']} "
            f"no_email_pref={counters['no_email_pref']}"
        )