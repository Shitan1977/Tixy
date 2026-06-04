import re
import time
from datetime import timedelta
from typing import Tuple

from django.conf import settings
from django.core.mail import send_mail
from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

# =============================================================================
# scan_pro_alerts_generic.py — VERSIONE DEFINITIVA
# =============================================================================
# PATCH 1 — vivaticket rimosso da _NOT_READY_PLATFORMS (ha scanner dedicato)
# PATCH 2 — Ticketmaster usa check_ticketmaster_mapping_availability
#            invece di check_ticketmaster_page_availability (solo HTML)
#            → rileva prezzi su shop.ticketmaster.it/partner/ e URL standard
# PATCH 3 — _is_reliable_available: strong_tokens PRIMA di weak_tokens
#            (fix bug browser_price_detected bloccato da weak_positive_keyword)
# PATCH 4 — skip performance già passate
# PATCH 5 — skip abbonamenti scaduti con attivo=True
# PATCH 6 — _extract_tm_code_from_url: gestione URL partner shop.ticketmaster.it
# PATCH 7 — anti rate-limit: skip se già scansionato nelle ultime N ore
# PATCH 8 — --only-email per test su singolo utente
# PATCH 9 — log riepilogo esteso
# =============================================================================

# Piattaforme non ancora supportate dallo scanner generico.
# vivaticket è RIMOSSO: ha uno scanner dedicato (scan_vivaticket_pro).
# fansale resta non supportato fino a implementazione checker.
_NOT_READY_PLATFORMS = {"fansale"}

# Anti rate-limit: skip mapping scansionato nelle ultime N ore
SKIP_IF_SCANNED_WITHIN_HOURS: int = 4


# =============================================================================
# Helpers URL e ID
# =============================================================================

def _extract_tm_code_from_url(url):
    """
    PATCH 6: estrae il codice evento TM dall'URL.
    Gestisce URL partner shop.ticketmaster.it/partner/biglietti/slug-15471.html
    estraendo solo il numero finale invece dell'intero slug.
    """
    if not url:
        return None
    clean_url = str(url).strip().rstrip("/")
    if not clean_url:
        return None
    if "/partner/" in clean_url:
        m = re.search(r"-(\d{4,6})\.html$", clean_url)
        if m:
            return m.group(1)
        return clean_url.split("/")[-1].split("?")[0].split("#")[0].strip()
    if "/event/" in clean_url:
        return clean_url.split("/event/")[-1].split("?")[0].split("#")[0].strip()
    return clean_url.split("/")[-1].split("?")[0].split("#")[0].strip()


def normalize_platform_name(name):
    if not name:
        return ""
    return str(name).strip().lower()


def get_link_url(link):
    url = getattr(link, "url", "")
    if not url:
        return ""
    return str(url).strip()


# =============================================================================
# Filtri abbonamento e performance
# =============================================================================

def _abbonamento_is_active(ab):
    """PATCH 5: controlla anche data_fine anche se attivo=True."""
    if not getattr(ab, "attivo", False):
        return False
    data_fine = getattr(ab, "data_fine", None)
    if data_fine and data_fine < timezone.now():
        return False
    return True


def _performance_is_past(performance):
    """PATCH 4: salta performance già passate."""
    starts_at = getattr(performance, "starts_at_utc", None)
    if starts_at and starts_at < timezone.now():
        return True
    return False


# =============================================================================
# Anti rate-limit
# =============================================================================

def _was_scanned_recently(mapping_type, mapping_pk, hours=SKIP_IF_SCANNED_WITHIN_HOURS):
    """
    PATCH 7: ritorna True se il mapping è stato scansionato nelle ultime `hours` ore.
    Legge ultima_scansione da PerformancePiattaforma o EventoPiattaforma.
    """
    if not mapping_type or not mapping_pk:
        return False

    from api.models import PerformancePiattaforma, EventoPiattaforma

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


def _touch_last_scan(mapping_type, mapping_pk):
    """Aggiorna ultima_scansione dopo ogni controllo esterno."""
    if not mapping_type or not mapping_pk:
        return

    from api.models import PerformancePiattaforma, EventoPiattaforma

    now = timezone.now()
    if mapping_type == "performance":
        PerformancePiattaforma.objects.filter(pk=mapping_pk).update(ultima_scansione=now)
    elif mapping_type == "evento":
        EventoPiattaforma.objects.filter(pk=mapping_pk).update(ultima_scansione=now)


# =============================================================================
# Segnali affidabili Ticketmaster
# =============================================================================

def _is_reliable_available_tm(availability, reason):
    """
    PATCH 3: strong_tokens controllati PRIMA di weak_tokens.
    Risolve il bug per cui browser_price_detected veniva bloccato
    dalla presenza di weak_positive_keyword nella stessa reason composta.
    """
    if availability != "available":
        return False

    reason_lower = (reason or "").lower().strip()
    if not reason_lower:
        return False

    strong_tokens = [
        "browser_price_detected",
        "strong_positive_keyword:aggiungi al carrello",
        "strong_positive_keyword:procedi all'acquisto",
        "strong_positive_keyword:procedi con l'acquisto",
        "strong_positive_keyword:seleziona biglietti",
        "strong_positive_keyword:scegli i biglietti",
        "resale_price_strong_signal",
        "html+prices",
    ]
    for token in strong_tokens:
        if token in reason_lower:
            return True

    weak_tokens = [
        "buy tickets", "acquista biglietti", "tickets available",
        "find tickets", "get tickets", "select tickets",
        "discovery_available_url_match", "weak_positive_keyword",
        "price_without_context", "no_strong_signals",
    ]
    for token in weak_tokens:
        if token in reason_lower:
            return False

    return False


# =============================================================================
# Performance equivalenti
# =============================================================================

def find_equivalent_performances(performance):
    """
    Cerca performance equivalenti per stesso evento/data/città.
    Serve per controllare Ticketmaster anche su monitoraggi nati da fansale.
    """
    if not performance:
        return []

    from api.models import Performance

    evento = performance.evento
    luogo = performance.luogo

    if not evento:
        return [performance]

    nome_norm = (evento.nome_evento_normalizzato or "").strip().lower()
    if not nome_norm:
        nome_norm = (evento.nome_evento or "").strip().lower()
    if not nome_norm:
        return [performance]

    start = performance.starts_at_utc - timedelta(hours=12)
    end = performance.starts_at_utc + timedelta(hours=12)

    qs = (
        Performance.objects
        .select_related("evento", "luogo")
        .filter(starts_at_utc__gte=start)
        .filter(starts_at_utc__lte=end)
        .filter(evento__nome_evento_normalizzato__iexact=nome_norm)
    )

    if luogo and luogo.citta:
        qs = qs.filter(luogo__citta__iexact=luogo.citta)

    performances = list(qs.order_by("id"))

    if performance not in performances:
        performances.insert(0, performance)

    return performances


# =============================================================================
# Checker per piattaforma
# =============================================================================

def check_platform_availability(platform_name, url, verbose=False,
                                 skip_scan_hours=SKIP_IF_SCANNED_WITHIN_HOURS):
    """
    Dispatcher generico per piattaforma.
    Ritorna sempre un dict con: ok, availability, reason, status_code, final_url.
    """
    if platform_name == "ticketmaster":
        return check_ticketmaster(url=url, verbose=verbose)

    if platform_name == "ticketone":
        return check_ticketone(url=url, verbose=verbose)

    if platform_name == "fansale":
        return {
            "ok": True, "availability": "unknown",
            "reason": "fansale_checker_not_ready",
            "status_code": None, "final_url": url,
        }

    if platform_name == "vivaticket":
        # vivaticket ha scanner dedicato — qui non dovrebbe arrivare
        return {
            "ok": True, "availability": "unknown",
            "reason": "vivaticket_use_dedicated_scanner",
            "status_code": None, "final_url": url,
        }

    return {
        "ok": True, "availability": "unknown",
        "reason": f"unsupported_platform_{platform_name}",
        "status_code": None, "final_url": url,
    }


def check_ticketmaster(url, verbose=False):
    """
    PATCH 2: usa check_ticketmaster_mapping_availability (HTML + Discovery API + browser)
    invece del solo check_ticketmaster_page_availability (HTML statico).
    Questo permette di rilevare prezzi su shop.ticketmaster.it/partner/ e URL standard.
    """
    from api.scrapers.ticketmaster_availability import check_ticketmaster_mapping_availability

    tm_id = _extract_tm_code_from_url(url)

    if not tm_id:
        return {
            "ok": False, "availability": "unknown",
            "reason": "ticketmaster_no_id_extracted",
            "status_code": None, "final_url": url,
        }

    try:
        res = check_ticketmaster_mapping_availability(tm_id=tm_id, url=url)
    except Exception as ex:
        return {
            "ok": False, "availability": "unknown",
            "reason": f"ticketmaster_exception:{ex}",
            "status_code": None, "final_url": url,
        }

    availability = res.get("availability", "unknown")
    reason = res.get("reason", "")
    status_code = res.get("status_code")

    # PATCH 3: verifica segnale affidabile per Ticketmaster
    if availability == "available" and not _is_reliable_available_tm(availability, reason):
        availability = "unknown"
        reason = f"weak_signal_downgraded:{reason}"

    return {
        "ok": res.get("ok", False),
        "availability": availability,
        "reason": reason,
        "status_code": status_code,
        "final_url": res.get("final_url", url),
        "price": res.get("price"),
        "raw": res,
    }


def check_ticketone(url, verbose=False):
    """
    Controllo TicketOne leggero senza browser fallback.
    Richiede min_price o raw_price_text per dichiarare available.
    """
    from api.scrapers.ticketone.ticketone_prices import get_ticketone_price_data

    try:
        price_data = get_ticketone_price_data(
            url,
            verbose=False,
            use_browser_fallback=False,
            browser_headless=True,
        )

        is_available = (
            price_data.get("min_price") is not None
            or bool(price_data.get("raw_price_text"))
        )

        reason = build_ticketone_reason(price_data)

        return {
            "ok": True,
            "availability": "available" if is_available else "unknown",
            "reason": reason,
            "status_code": price_data.get("status_code"),
            "final_url": price_data.get("final_url", url),
            "min_price": price_data.get("min_price"),
            "currency": price_data.get("currency"),
            "raw_price_text": price_data.get("raw_price_text"),
            "raw": price_data,
        }

    except Exception as exc:
        return {
            "ok": False, "availability": "unknown",
            "reason": f"ticketone_exception:{exc}",
            "status_code": None, "final_url": url,
        }


def build_ticketone_reason(result):
    if result.get("min_price") is not None:
        return "ticketone_min_price_found"
    if result.get("raw_price_text"):
        return "ticketone_raw_price_text_found"
    if result.get("detail_status") == "ok":
        return "ticketone_detail_status_ok"
    detail_status = result.get("detail_status") or "no_detail_status"
    source_used = result.get("source_used") or "no_source"
    return f"ticketone_no_strong_signal:{detail_status}:{source_used}"


# =============================================================================
# Email
# =============================================================================

def send_email_with_retry(*, subject, message, to_email, max_retries, base_wait):
    last_err = ""
    for attempt in range(1, max_retries + 1):
        try:
            send_mail(
                subject=subject, message=message,
                from_email=getattr(settings, "DEFAULT_FROM_EMAIL", None),
                recipient_list=[to_email], fail_silently=False,
            )
            return True, ""
        except Exception as exc:
            last_err = str(exc)
            if attempt < max_retries:
                time.sleep(base_wait * attempt)
    return False, last_err


def build_generic_dedupe_key(*, monitoraggio, user, platform_name):
    """
    Chiave giornaliera per deduplicare notifiche.
    Formato: generic:{platform}:mon:{mon_id}:user:{user_id}:{YYYY-MM-DD}
    La reason è esclusa intenzionalmente: stesso evento/piattaforma/giorno
    non deve generare più email anche se la reason cambia tra run.
    """
    day = timezone.now().date().isoformat()
    return f"generic:{platform_name}:mon:{monitoraggio.id}:user:{user.id}:{day}"


def build_generic_email_message(*, user, monitoraggio, performance, evento,
                                 platform_name, url, result):
    event_name = evento.nome_evento if evento else "Evento monitorato"
    luogo = "Luogo non disponibile"
    data_evento = "Data non disponibile"

    if performance:
        if performance.luogo:
            luogo = performance.luogo.nome
        if performance.starts_at_utc:
            data_evento = performance.starts_at_utc.strftime("%d/%m/%Y %H:%M")

    platform_label = platform_name.upper()
    subject = f"[Tixy] Biglietti disponibili su {platform_label} - {event_name}"

    min_price = result.get("min_price")
    currency = result.get("currency") or "EUR"
    raw_price_text = result.get("raw_price_text")
    price = result.get("price")
    reason = result.get("reason") or "available"

    message = (
        f"Ciao {getattr(user, 'first_name', '') or ''},\n\n"
        f"abbiamo trovato una disponibilità per il tuo monitoraggio PRO.\n\n"
        f"Evento: {event_name}\n"
        f"Luogo: {luogo}\n"
        f"Data: {data_evento}\n"
        f"Piattaforma: {platform_label}\n\n"
    )

    if price:
        message += f"Prezzo rilevato: {price}\n"
    elif min_price is not None:
        message += f"Prezzo rilevato: da {min_price} {currency}\n"
    elif raw_price_text:
        message += f"Prezzo rilevato: {raw_price_text}\n"
    else:
        message += "Prezzo: non disponibile\n"

    message += f"\nLink:\n{url}\n\nGrazie,\nTixy\n"

    return subject, message


# =============================================================================
# Command
# =============================================================================

class Command(BaseCommand):
    help = "Scanner generico PRO: controlla disponibilità biglietti su tutte le piattaforme collegate."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=200,
                            help="Numero massimo di monitoraggi da processare.")
        parser.add_argument("--sleep", type=float, default=0.0,
                            help="Pausa (secondi) tra un controllo e l'altro.")
        parser.add_argument("--verbose", action="store_true",
                            help="Log dettagliato.")
        parser.add_argument("--dry-run", action="store_true",
                            help="Non invia email e non salva notifiche.")
        parser.add_argument("--force-available-platform", type=str, default=None,
                            help="Solo test: forza una piattaforma a risultare available.")
        parser.add_argument("--email-retries", type=int, default=3)
        parser.add_argument("--email-wait", type=float, default=1.5)
        parser.add_argument("--only-email", type=str, default=None,
                            help="PATCH 8: processa solo i monitoraggi di questo utente. Per test.")
        parser.add_argument("--skip-scan-hours", type=int, default=SKIP_IF_SCANNED_WITHIN_HOURS,
                            help="PATCH 7: skip mapping scansionato nelle ultime N ore. 0=disabilitato.")
        parser.add_argument("--include-past", action="store_true",
                            help="Includi performance già passate (default: skip).")

    def handle(self, *args, **options):
        limit = options["limit"]
        sleep_seconds = options["sleep"]
        verbose = options["verbose"]
        dry_run = options["dry_run"]
        force_available_platform = options.get("force_available_platform")
        email_retries = max(1, int(options.get("email_retries") or 3))
        email_wait = max(0.5, float(options.get("email_wait") or 1.5))
        skip_scan_hours = max(0, int(options.get("skip_scan_hours") or 0))
        include_past = bool(options.get("include_past", False))
        only_email = (options.get("only_email") or "").strip().lower() or None

        if force_available_platform:
            force_available_platform = force_available_platform.strip().lower()

        now = timezone.now()

        self.stdout.write(self.style.SUCCESS("[START] scan_pro_alerts_generic"))
        self.stdout.write(f"[TIME] {now.isoformat()}")
        self.stdout.write(
            f"[CONFIG] limit={limit} sleep={sleep_seconds} dry_run={dry_run} "
            f"skip_scan_hours={skip_scan_hours} include_past={include_past}"
        )

        from api.models import (
            Monitoraggio,
            EventoPiattaforma,
            PerformancePiattaforma,
            Notifica,
        )

        qs = (
            Monitoraggio.objects
            .select_related(
                "abbonamento", "abbonamento__utente", "abbonamento__plan",
                "performance", "performance__evento", "performance__luogo",
                "evento",
            )
            .filter(abbonamento__attivo=True)
            .filter(abbonamento__plan__plan_type="PRO")
            .filter(abbonamento__prezzo__gt=0)
            .filter(
                Q(abbonamento__data_fine__isnull=True) |
                Q(abbonamento__data_fine__gte=now)
            )
            .order_by("id")
        )

        if only_email:
            qs = qs.filter(abbonamento__utente__email__iexact=only_email)

        # PATCH 5: filtra abbonamenti davvero attivi (data_fine check)
        monitoraggi = []
        for m in qs[: limit * 5]:
            try:
                if _abbonamento_is_active(m.abbonamento):
                    monitoraggi.append(m)
            except Exception:
                continue
            if len(monitoraggi) >= limit:
                break

        self.stdout.write(f"[PRO] monitoraggi PRO attivi trovati: {len(monitoraggi)}")

        # Contatori
        processed = 0
        skipped_no_target = 0
        skipped_no_platform = 0
        skipped_past = 0
        links_found = 0
        ticketmaster_count = 0
        ticketone_count = 0
        fansale_count = 0
        vivaticket_count = 0
        other_count = 0
        notified = 0
        deduped = 0
        unknown_count = 0
        unavailable_count = 0
        skipped_not_ready = 0
        skipped_rate_limit = 0
        email_fail_count = 0

        for monitoraggio in monitoraggi:
            processed += 1

            abbonamento = monitoraggio.abbonamento
            utente = abbonamento.utente
            performance = monitoraggio.performance
            evento = monitoraggio.evento

            if performance and not evento:
                evento = performance.evento

            if not performance and not evento:
                skipped_no_target += 1
                if verbose:
                    self.stdout.write(self.style.WARNING(
                        f"[SKIP] monitoraggio={monitoraggio.id}: nessun evento/performance"
                    ))
                continue

            # PATCH 4: skip performance passate
            if performance and not include_past and _performance_is_past(performance):
                skipped_past += 1
                if verbose:
                    self.stdout.write(self.style.WARNING(
                        f"[SKIP PAST] monitoraggio={monitoraggio.id} perf={performance.id}: evento già passato"
                    ))
                continue

            if verbose:
                self.stdout.write("")
                self.stdout.write(
                    f"[MONITORAGGIO] id={monitoraggio.id} "
                    f"email={utente.email} "
                    f"performance_id={performance.id if performance else None} "
                    f"evento_id={evento.id if evento else None}"
                )
                if evento:
                    self.stdout.write(f"[EVENTO] {evento.nome_evento}")
                if performance:
                    luogo_nome = performance.luogo.nome if performance.luogo else "-"
                    self.stdout.write(
                        f"[PERFORMANCE] id={performance.id} "
                        f"data={performance.starts_at_utc} luogo={luogo_nome}"
                    )

            # Raccoglie tutti i link piattaforma (performance + equivalenti + evento)
            platform_links = []
            seen_links = set()

            def add_platform_link(source, link):
                platform_name = normalize_platform_name(link.piattaforma.nome)
                url = get_link_url(link)
                if not platform_name and not url:
                    return
                key = (platform_name, url)
                if key in seen_links:
                    return
                seen_links.add(key)
                platform_links.append({
                    "source": source,
                    "platform_name": platform_name,
                    "platform_id": link.piattaforma_id,
                    "url": url,
                    "mapping_pk": link.pk,
                    "mapping_type": "performance" if "performance" in source else "evento",
                })

            equivalent_performances = []
            if performance:
                equivalent_performances = find_equivalent_performances(performance)

            equivalent_event_ids = set()
            if evento:
                equivalent_event_ids.add(evento.id)

            for eq_perf in equivalent_performances:
                if eq_perf.evento_id:
                    equivalent_event_ids.add(eq_perf.evento_id)
                perf_links = (
                    PerformancePiattaforma.objects
                    .select_related("piattaforma")
                    .filter(performance=eq_perf, piattaforma__attivo=True)
                )
                for link in perf_links:
                    add_platform_link(f"performance:{eq_perf.id}", link)

            if equivalent_event_ids:
                event_links = (
                    EventoPiattaforma.objects
                    .select_related("piattaforma")
                    .filter(evento_id__in=equivalent_event_ids, piattaforma__attivo=True)
                )
                for link in event_links:
                    add_platform_link(f"evento:{link.evento_id}", link)

            if not platform_links:
                skipped_no_platform += 1
                if verbose:
                    self.stdout.write(self.style.WARNING(
                        f"[SKIP] monitoraggio={monitoraggio.id}: nessuna piattaforma collegata"
                    ))
                continue

            for link in platform_links:
                links_found += 1
                platform_name = link["platform_name"]
                url = link["url"]
                source = link["source"]
                mapping_pk = link["mapping_pk"]
                mapping_type = link["mapping_type"]

                if platform_name == "ticketmaster":
                    ticketmaster_count += 1
                elif platform_name == "ticketone":
                    ticketone_count += 1
                elif platform_name == "fansale":
                    fansale_count += 1
                elif platform_name == "vivaticket":
                    vivaticket_count += 1
                else:
                    other_count += 1

                if not url:
                    if verbose:
                        self.stdout.write(self.style.WARNING(
                            f"[SKIP URL] monitoraggio={monitoraggio.id} platform={platform_name}: url vuoto"
                        ))
                    continue

                # Skip piattaforme non supportate
                if platform_name in _NOT_READY_PLATFORMS:
                    skipped_not_ready += 1
                    if verbose:
                        self.stdout.write(self.style.WARNING(
                            f"[SKIP NOT READY] platform={platform_name} monitoraggio={monitoraggio.id}"
                        ))
                    if sleep_seconds > 0:
                        time.sleep(sleep_seconds)
                    continue

                # PATCH 7: anti rate-limit
                if skip_scan_hours > 0 and _was_scanned_recently(mapping_type, mapping_pk, skip_scan_hours):
                    skipped_rate_limit += 1
                    if verbose:
                        self.stdout.write(
                            f"[SKIP RATE] platform={platform_name} monitoraggio={monitoraggio.id}: "
                            f"già scansionato nelle ultime {skip_scan_hours}h"
                        )
                    continue

                if verbose:
                    self.stdout.write(f"[LINK] source={source} platform={platform_name} url={url}")

                # Chiama il checker
                result = check_platform_availability(
                    platform_name=platform_name,
                    url=url,
                    verbose=verbose,
                )

                # Aggiorna ultima_scansione
                _touch_last_scan(mapping_type, mapping_pk)

                # Force available (solo test)
                if force_available_platform and platform_name == force_available_platform:
                    result = {
                        "ok": True, "availability": "available",
                        "reason": "FORCE_AVAILABLE",
                        "status_code": result.get("status_code"),
                        "final_url": result.get("final_url", url),
                        "min_price": result.get("min_price"),
                        "currency": result.get("currency"),
                        "raw_price_text": result.get("raw_price_text"),
                        "price": result.get("price"),
                    }
                    self.stdout.write(self.style.WARNING(
                        f"[FORCE AVAILABLE] platform={platform_name} monitoraggio={monitoraggio.id}"
                    ))

                status_code = result.get("status_code")

                self.stdout.write(
                    f"[RESULT] platform={platform_name} "
                    f"availability={result['availability']} "
                    f"reason={result['reason']} "
                    f"status_code={status_code}"
                )

                # HTTP 401: skip silenzioso
                if status_code == 401:
                    unknown_count += 1
                    if verbose:
                        self.stdout.write(self.style.WARNING(
                            f"[SKIP 401] platform={platform_name} monitoraggio={monitoraggio.id}"
                        ))
                    if sleep_seconds > 0:
                        time.sleep(sleep_seconds)
                    continue

                if result["availability"] == "available":
                    dedupe_key = build_generic_dedupe_key(
                        monitoraggio=monitoraggio,
                        user=utente,
                        platform_name=platform_name,
                    )

                    if Notifica.objects.filter(dedupe_key=dedupe_key, status="SENT").exists():
                        deduped += 1
                        self.stdout.write(self.style.WARNING(
                            f"[DEDUP] monitoraggio={monitoraggio.id} "
                            f"platform={platform_name} dedupe={dedupe_key}"
                        ))
                        if sleep_seconds > 0:
                            time.sleep(sleep_seconds)
                        continue

                    subject, message = build_generic_email_message(
                        user=utente, monitoraggio=monitoraggio,
                        performance=performance, evento=evento,
                        platform_name=platform_name, url=url, result=result,
                    )

                    if dry_run:
                        self.stdout.write(self.style.WARNING(
                            f"[DRY-RUN EMAIL] to={utente.email} "
                            f"subject={subject} dedupe={dedupe_key}"
                        ))
                        notified += 1
                    else:
                        if not getattr(utente, "notify_email", True):
                            if verbose:
                                self.stdout.write(self.style.WARNING(
                                    f"[SKIP EMAIL PREF] user={utente.id}"
                                ))
                            if sleep_seconds > 0:
                                time.sleep(sleep_seconds)
                            continue

                        to_email = getattr(utente, "email", None)
                        if not to_email:
                            if verbose:
                                self.stdout.write(self.style.WARNING(
                                    f"[SKIP NO EMAIL] user={utente.id}"
                                ))
                            if sleep_seconds > 0:
                                time.sleep(sleep_seconds)
                            continue

                        ok, err = send_email_with_retry(
                            subject=subject, message=message,
                            to_email=to_email,
                            max_retries=email_retries, base_wait=email_wait,
                        )

                        with transaction.atomic():
                            if ok:
                                Notifica.objects.create(
                                    monitoraggio=monitoraggio,
                                    channel="email",
                                    dedupe_key=dedupe_key,
                                    status="SENT",
                                    sent_at=timezone.now(),
                                    message=message,
                                )
                                notified += 1
                                self.stdout.write(self.style.SUCCESS(
                                    f"[EMAIL SENT] monitoraggio={monitoraggio.id} "
                                    f"to={to_email} dedupe={dedupe_key}"
                                ))
                            else:
                                email_fail_count += 1
                                Notifica.objects.create(
                                    monitoraggio=monitoraggio,
                                    channel="email",
                                    dedupe_key=dedupe_key,
                                    status="FAILED",
                                    message=f"{message}\n\nERRORE:\n{err}",
                                )
                                self.stdout.write(self.style.ERROR(
                                    f"[EMAIL FAIL] monitoraggio={monitoraggio.id} "
                                    f"to={to_email} err={err}"
                                ))

                elif result["availability"] == "unavailable":
                    unavailable_count += 1
                else:
                    unknown_count += 1

                if sleep_seconds > 0:
                    time.sleep(sleep_seconds)

        # PATCH 9: riepilogo finale esteso
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("[DONE]"))
        self.stdout.write(f"processed            ={processed}")
        self.stdout.write(f"links_found          ={links_found}")
        self.stdout.write(f"  ticketmaster_links  ={ticketmaster_count}")
        self.stdout.write(f"  ticketone_links     ={ticketone_count}")
        self.stdout.write(f"  fansale_links       ={fansale_count}")
        self.stdout.write(f"  vivaticket_links    ={vivaticket_count}")
        self.stdout.write(f"  other_links         ={other_count}")
        self.stdout.write("")
        self.stdout.write(f"notified             ={notified}")
        self.stdout.write(f"deduped              ={deduped}")
        self.stdout.write(f"unknown              ={unknown_count}")
        self.stdout.write(f"unavailable          ={unavailable_count}")
        self.stdout.write(f"email_fail           ={email_fail_count}")
        self.stdout.write(f"skipped_not_ready    ={skipped_not_ready}")
        self.stdout.write(f"skipped_rate_limit   ={skipped_rate_limit}")
        self.stdout.write(f"skipped_past         ={skipped_past}")
        self.stdout.write(f"skipped_no_target    ={skipped_no_target}")
        self.stdout.write(f"skipped_no_platform  ={skipped_no_platform}")