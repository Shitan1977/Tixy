# =============================================================================
# scan_pro_alerts_generic.py — v2 (refactoring completo, 2026-07-06)
# =============================================================================
# Design:
#   1. select_monitoraggi()  - selezione e filtri (PRO, attivi, first-scan)
#   2. collect_links()       - link performance (prioritari) + fallback evento
#   3. decisione check       - rate-limit, cache multi-utente, freshness
#   4. checker per piattaforma (dispatcher invariato nella sostanza)
#   5. decide_alert()        - dedupe giornaliero (default) o cooldown re-alert
#   6. notify()              - email con retry + Notifica SENT/FAILED + backoff
#
# Principio cardine: MEGLIO PERDERE UN ALERT VERO CHE MANDARE UN FALSO POSITIVO.
# - vivaticket: snapshot dal DB, MAI HTTP, MAI touch di ultima_scansione
#   (il timestamp e' scritto SOLO da refresh_vivaticket_mapped = eta' del dato);
#   guardia freschezza: snapshot piu' vecchio di N ore -> unknown, zero alert.
# - exclude() JSON NULL-safe: filtro invalidi in Python.
# - FAILED backoff: niente tempesta di retry se SMTP rotto.
# Flag nuovi (default = comportamento legacy):
#   --only-new-minutes N : first-scan solo monitoraggi creati negli ultimi N
#                          minuti, rate-limit ignorato (per cron dedicato).
#   --realert-hours N    : N>0 abilita re-alert con cooldown di N ore al posto
#                          del dedupe giornaliero. Default 0 = legacy.
# =============================================================================

import re
import time
from datetime import timedelta

from django.conf import settings
from django.core.mail import send_mail
from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

SKIP_IF_SCANNED_WITHIN_HOURS = 4
VIVATICKET_SNAPSHOT_MAX_AGE_HOURS = 2
FAILED_BACKOFF_MINUTES = 30
_NOT_READY_PLATFORMS = set()


# ----------------------------------------------------------------- helpers --

def _extract_tm_code_from_url(url):
    # Estrae il codice evento TM (gestisce anche shop.ticketmaster.it/partner/)
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


def _abbonamento_is_active(ab):
    if not getattr(ab, "attivo", False):
        return False
    data_fine = getattr(ab, "data_fine", None)
    if data_fine and data_fine < timezone.now():
        return False
    return True


def _performance_is_past(performance):
    starts_at = getattr(performance, "starts_at_utc", None)
    return bool(starts_at and starts_at < timezone.now())


# -------------------------------------------------------------- rate limit --

def _was_scanned_recently(mapping_type, mapping_pk, hours=SKIP_IF_SCANNED_WITHIN_HOURS):
    if not mapping_type or not mapping_pk:
        return False
    from api.models import PerformancePiattaforma, EventoPiattaforma
    threshold = timezone.now() - timedelta(hours=hours)
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
    # NB: chiamato SOLO per piattaforme con controllo HTTP reale (mai vivaticket)
    if not mapping_type or not mapping_pk:
        return
    from api.models import PerformancePiattaforma, EventoPiattaforma
    now = timezone.now()
    if mapping_type == "performance":
        PerformancePiattaforma.objects.filter(pk=mapping_pk).update(ultima_scansione=now)
    elif mapping_type == "evento":
        EventoPiattaforma.objects.filter(pk=mapping_pk).update(ultima_scansione=now)


# ------------------------------------------------------ segnali affidabili --

def _is_reliable_available_tm(availability, reason):
    # strong tokens PRIMA dei weak (fix storico browser_price_detected)
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


# --------------------------------------------------- performance equivalenti --

def find_equivalent_performances(performance):
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
        .filter(starts_at_utc__gte=start, starts_at_utc__lte=end)
        .filter(evento__nome_evento_normalizzato__iexact=nome_norm)
    )
    if luogo and luogo.citta:
        qs = qs.filter(luogo__citta__iexact=luogo.citta)
    performances = list(qs.order_by("id"))
    if performance not in performances:
        performances.insert(0, performance)
    return performances


# ------------------------------------------------------------------ checkers --

def check_vivaticket(url, mapping_pk=None, mapping_type=None):
    # Snapshot dal DB, zero HTTP. ultima_scansione = eta' del dato (solo refresh la scrive).
    from api.models import PerformancePiattaforma, EventoPiattaforma
    pp = None
    if mapping_type == "performance" and mapping_pk:
        pp = PerformancePiattaforma.objects.filter(
            pk=mapping_pk, piattaforma__nome__iexact="vivaticket").first()
    if not pp and mapping_type == "evento" and mapping_pk:
        ep = EventoPiattaforma.objects.filter(pk=mapping_pk).select_related("evento").first()
        if ep and ep.evento_id:
            pp = (PerformancePiattaforma.objects
                  .filter(performance__evento_id=ep.evento_id,
                          piattaforma__nome__iexact="vivaticket")
                  .first())
    if not pp:
        pp = PerformancePiattaforma.objects.filter(
            url=url, piattaforma__nome__iexact="vivaticket").first()
    if not pp:
        return {"ok": True, "availability": "unknown",
                "reason": "vivaticket_pp_not_found",
                "status_code": None, "final_url": url}

    # GUARDIA FRESCHEZZA: dato piu' vecchio del limite -> unknown, zero alert.
    max_age = timedelta(hours=VIVATICKET_SNAPSHOT_MAX_AGE_HOURS)
    if not pp.ultima_scansione or pp.ultima_scansione < timezone.now() - max_age:
        return {"ok": True, "availability": "unknown",
                "reason": "vivaticket_snapshot_stale:%s" % pp.ultima_scansione,
                "status_code": None, "final_url": url}

    snap = pp.snapshot_raw or {}
    sale_status = snap.get("sale_status")
    resale_link = (snap.get("resale_link") or "").strip()
    final_url = url
    is_resale_alert = False

    if sale_status in ("available", "available_or_special"):
        availability = "available"
        reason = "vivaticket_snapshot:%s" % sale_status
    elif sale_status in ("sold_out", "inactive_sell_button", "no_sell_button"):
        if resale_link:
            availability = "available"
            reason = "vivaticket_resale_only:%s" % sale_status
            final_url = resale_link
            is_resale_alert = True
        else:
            availability = "unavailable"
            reason = "vivaticket_snapshot:%s" % sale_status
    else:
        availability = "unknown"
        reason = "vivaticket_snapshot:%s" % sale_status

    return {"ok": True, "availability": availability, "reason": reason,
            "status_code": None, "final_url": final_url,
            "is_resale": is_resale_alert}


def check_ticketmaster(url, verbose=False):
    from api.scrapers.ticketmaster_availability import check_ticketmaster_mapping_availability
    tm_id = _extract_tm_code_from_url(url)
    if not tm_id:
        return {"ok": False, "availability": "unknown",
                "reason": "ticketmaster_no_id_extracted",
                "status_code": None, "final_url": url}
    try:
        res = check_ticketmaster_mapping_availability(tm_id=tm_id, url=url)
    except Exception as ex:
        return {"ok": False, "availability": "unknown",
                "reason": "ticketmaster_exception:%s" % ex,
                "status_code": None, "final_url": url}
    availability = res.get("availability", "unknown")
    reason = res.get("reason", "")
    if availability == "available" and not _is_reliable_available_tm(availability, reason):
        availability = "unknown"
        reason = "weak_signal_downgraded:%s" % reason
    return {"ok": res.get("ok", False), "availability": availability,
            "reason": reason, "status_code": res.get("status_code"),
            "final_url": res.get("final_url", url),
            "price": res.get("price"), "raw": res}


def build_ticketone_reason(result):
    if result.get("min_price") is not None:
        return "ticketone_min_price_found"
    if result.get("raw_price_text"):
        return "ticketone_raw_price_text_found"
    if result.get("detail_status") == "ok":
        return "ticketone_detail_status_ok"
    detail_status = result.get("detail_status") or "no_detail_status"
    source_used = result.get("source_used") or "no_source"
    return "ticketone_no_strong_signal:%s:%s" % (detail_status, source_used)


def check_ticketone(url, verbose=False):
    from api.scrapers.ticketone.ticketone_prices import get_ticketone_price_data
    try:
        price_data = get_ticketone_price_data(
            url, verbose=False, use_browser_fallback=False, browser_headless=True)
        is_available = (price_data.get("min_price") is not None
                        or bool(price_data.get("raw_price_text")))
        return {"ok": True,
                "availability": "available" if is_available else "unknown",
                "reason": build_ticketone_reason(price_data),
                "status_code": price_data.get("status_code"),
                "final_url": price_data.get("final_url", url),
                "min_price": price_data.get("min_price"),
                "currency": price_data.get("currency"),
                "raw_price_text": price_data.get("raw_price_text"),
                "raw": price_data}
    except Exception as exc:
        return {"ok": False, "availability": "unknown",
                "reason": "ticketone_exception:%s" % exc,
                "status_code": None, "final_url": url}


def check_fansale(url, verbose=False):
    from api.scrapers.fansale_checker import check_fansale_availability
    result = check_fansale_availability(url=url, verbose=verbose)
    return {"ok": result.get("ok", False),
            "availability": result.get("availability", "unknown"),
            "reason": result.get("reason", "fansale_unknown"),
            "status_code": None,
            "final_url": result.get("url", url),
            "min_price": result.get("min_price")}


def check_platform_availability(platform_name, url, verbose=False,
                                mapping_pk=None, mapping_type=None):
    if platform_name == "ticketmaster":
        return check_ticketmaster(url=url, verbose=verbose)
    if platform_name == "ticketone":
        return check_ticketone(url=url, verbose=verbose)
    if platform_name == "fansale":
        return check_fansale(url=url, verbose=verbose)
    if platform_name == "vivaticket":
        return check_vivaticket(url=url, mapping_pk=mapping_pk, mapping_type=mapping_type)
    return {"ok": True, "availability": "unknown",
            "reason": "unsupported_platform_%s" % platform_name,
            "status_code": None, "final_url": url}


# --------------------------------------------------------------------- email --

def send_email_with_retry(subject, message, to_email, max_retries, base_wait):
    last_err = ""
    for attempt in range(1, max_retries + 1):
        try:
            send_mail(subject=subject, message=message,
                      from_email=getattr(settings, "DEFAULT_FROM_EMAIL", None),
                      recipient_list=[to_email], fail_silently=False)
            return True, ""
        except Exception as exc:
            last_err = str(exc)
            if attempt < max_retries:
                time.sleep(base_wait * attempt)
    return False, last_err


def dedupe_prefix(monitoraggio, user, platform_name):
    return "generic:%s:mon:%s:user:%s" % (platform_name, monitoraggio.id, user.id)


def build_dedupe_key(monitoraggio, user, platform_name, realert_hours):
    prefix = dedupe_prefix(monitoraggio, user, platform_name)
    if realert_hours and realert_hours > 0:
        # chiave unica per invio (il cooldown decide, non la chiave)
        return "%s:%s" % (prefix, timezone.now().strftime("%Y-%m-%dT%H%M"))
    return "%s:%s" % (prefix, timezone.now().date().isoformat())


def build_generic_email_message(user, monitoraggio, performance, evento,
                                platform_name, url, result):
    event_name = evento.nome_evento if evento else "Evento monitorato"
    luogo = "Luogo non disponibile"
    data_evento = "Data non disponibile"
    if performance:
        if performance.luogo:
            luogo = performance.luogo.nome
        if performance.starts_at_utc:
            import pytz as _pytz
            _rome = _pytz.timezone("Europe/Rome")
            data_evento = performance.starts_at_utc.astimezone(_rome).strftime("%d/%m/%Y %H:%M")
    platform_label = platform_name.upper()
    is_resale = bool(result.get("is_resale"))
    if is_resale:
        subject = "[Tixy] Biglietti disponibili in RIVENDITA su %s - %s" % (platform_label, event_name)
        intro = "abbiamo trovato biglietti in RIVENDITA per il tuo monitoraggio PRO.\n\n"
    else:
        subject = "[Tixy] Biglietti disponibili su %s - %s" % (platform_label, event_name)
        intro = "abbiamo trovato una disponibilità per il tuo monitoraggio PRO.\n\n"
    message = ("Ciao %s,\n\n" % (getattr(user, "first_name", "") or "")) + intro
    message += "Evento: %s\nLuogo: %s\nData: %s\nPiattaforma: %s\n" % (
        event_name, luogo, data_evento, platform_label)
    message += ("Tipo: RIVENDITA (resale)\n\n" if is_resale else "\n")
    price = result.get("price")
    min_price = result.get("min_price")
    raw_price_text = result.get("raw_price_text")
    currency = result.get("currency") or "EUR"
    if price:
        message += "Prezzo rilevato: %s\n" % price
    elif min_price is not None:
        message += "Prezzo rilevato: da %s %s\n" % (min_price, currency)
    elif raw_price_text:
        message += "Prezzo rilevato: %s\n" % raw_price_text
    else:
        message += "Prezzo: non disponibile\n"
    message += "\nLink:\n%s\n\nGrazie,\nTixy\n" % url
    return subject, message


def build_multi_email_message(user, monitoraggio, performance, evento, availabilities):
    # Una sola email con tutte le piattaforme disponibili in questo giro.
    event_name = evento.nome_evento if evento else "Evento monitorato"
    luogo = "Luogo non disponibile"
    data_evento = "Data non disponibile"
    if performance:
        if performance.luogo:
            luogo = performance.luogo.nome
        if performance.starts_at_utc:
            import pytz as _pytz
            _rome = _pytz.timezone("Europe/Rome")
            data_evento = performance.starts_at_utc.astimezone(_rome).strftime("%d/%m/%Y %H:%M")
    labels = [a["platform_name"].upper() + (" (RIVENDITA)" if a["result"].get("is_resale") else "") for a in availabilities]
    plats = labels[0] if len(labels) == 1 else ", ".join(labels[:-1]) + " e " + labels[-1]
    subject = "[Tixy] Biglietti disponibili su %s - %s" % (plats, event_name)
    message = "Ciao %s,\n\n" % (getattr(user, "first_name", "") or "")
    message += "abbiamo trovato disponibilita' per il tuo monitoraggio PRO.\n\n"
    message += "Evento: %s\nLuogo: %s\nData: %s\n\n" % (event_name, luogo, data_evento)
    for a in availabilities:
        r = a["result"]
        message += "--- %s%s ---\n" % (a["platform_name"].upper(), " (RIVENDITA)" if r.get("is_resale") else "")
        price = r.get("price")
        min_price = r.get("min_price")
        raw_price_text = r.get("raw_price_text")
        currency = r.get("currency") or "EUR"
        if price:
            message += "Prezzo: %s\n" % price
        elif min_price is not None:
            message += "Prezzo: da %s %s\n" % (min_price, currency)
        elif raw_price_text:
            message += "Prezzo: %s\n" % raw_price_text
        message += "Link: %s\n\n" % a["email_url"]
    message += "Grazie,\nTixy\n"
    return subject, message


# ------------------------------------------------------------------- command --

class Command(BaseCommand):
    help = "Scanner generico PRO v2: disponibilita' biglietti su tutte le piattaforme collegate."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=200)
        parser.add_argument("--sleep", type=float, default=0.0)
        parser.add_argument("--verbose", action="store_true")
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--force-available-platform", type=str, default=None)
        parser.add_argument("--email-retries", type=int, default=3)
        parser.add_argument("--email-wait", type=float, default=1.5)
        parser.add_argument("--only-email", type=str, default=None)
        parser.add_argument("--skip-scan-hours", type=int, default=SKIP_IF_SCANNED_WITHIN_HOURS)
        parser.add_argument("--include-past", action="store_true")
        parser.add_argument("--only-new-minutes", type=int, default=0,
                            help="First-scan: solo monitoraggi creati negli ultimi N minuti, rate-limit ignorato. 0=off.")
        parser.add_argument("--realert-hours", type=int, default=0,
                            help="Re-alert con cooldown N ore invece del dedupe giornaliero. 0=legacy giornaliero.")

    # ------------------------------------------------- fase 1: selezione ----
    def select_monitoraggi(self, options, counters):
        from api.models import Monitoraggio
        now = timezone.now()
        qs = (Monitoraggio.objects
              .select_related("abbonamento", "abbonamento__utente", "abbonamento__plan",
                              "performance", "performance__evento", "performance__luogo",
                              "evento")
              .filter(abbonamento__attivo=True)
              .filter(abbonamento__prezzo__gt=0)
              # plan spesso NULL (bug storico F1): prezzo>0 e' il proxy PRO affidabile
              .filter(Q(abbonamento__data_fine__isnull=True) |
                      Q(abbonamento__data_fine__gte=now))
              .order_by("id"))
        only_email = (options.get("only_email") or "").strip().lower() or None
        if only_email:
            qs = qs.filter(abbonamento__utente__email__iexact=only_email)
        only_new_minutes = int(options.get("only_new_minutes") or 0)
        if only_new_minutes > 0:
            try:
                qs = qs.filter(creato_il__gte=now - timedelta(minutes=only_new_minutes))
            except Exception:
                self.stdout.write(self.style.WARNING(
                    "[WARN] campo creato_il assente su Monitoraggio: --only-new-minutes ignorato"))
        limit = options["limit"]
        monitoraggi = []
        for m in qs[: limit * 5]:
            try:
                if _abbonamento_is_active(m.abbonamento):
                    monitoraggi.append(m)
                else:
                    counters["skipped_expired"] += 1
            except Exception:
                continue
            if len(monitoraggi) >= limit:
                break
        return monitoraggi

    # ---------------------------------------------- fase 2: raccolta link ----
    def collect_links(self, monitoraggio, performance, evento):
        from api.models import PerformancePiattaforma, EventoPiattaforma
        platform_links = []
        seen_links = set()

        def add_platform_link(source, link):
            pname = normalize_platform_name(link.piattaforma.nome)
            url = get_link_url(link)
            if not pname and not url:
                return
            key = (pname, url)
            if key in seen_links:
                return
            seen_links.add(key)
            platform_links.append({
                "source": source, "platform_name": pname,
                "platform_id": link.piattaforma_id, "url": url,
                "mapping_pk": link.pk,
                "mapping_type": "performance" if "performance" in source else "evento",
            })

        equivalent_performances = find_equivalent_performances(performance) if performance else []
        equivalent_event_ids = set()
        if evento:
            equivalent_event_ids.add(evento.id)

        platforms_with_perf_link = set()
        for eq_perf in equivalent_performances:
            if eq_perf.evento_id:
                equivalent_event_ids.add(eq_perf.evento_id)
            # exclude() su chiave JSON scarta anche i NULL: filtro in Python
            perf_links = [
                l for l in PerformancePiattaforma.objects
                .select_related("piattaforma")
                .filter(performance=eq_perf, piattaforma__attivo=True)
                if (l.snapshot_raw or {}).get("status") != "invalid_url_no_id"
            ]
            for link in perf_links:
                pname = normalize_platform_name(link.piattaforma.nome)
                if get_link_url(link):
                    platforms_with_perf_link.add(pname)
                add_platform_link("performance:%s" % eq_perf.id, link)

        if equivalent_event_ids:
            event_links = (EventoPiattaforma.objects
                           .select_related("piattaforma")
                           .filter(evento_id__in=equivalent_event_ids,
                                   piattaforma__attivo=True))
            for link in event_links:
                pname = normalize_platform_name(link.piattaforma.nome)
                if pname in platforms_with_perf_link:
                    continue  # il link performance e' piu' specifico e vince
                add_platform_link("evento:%s" % link.evento_id, link)

        return platform_links

    # -------------------------------------------- fase 5: decisione alert ----
    def decide_alert(self, monitoraggio, utente, platform_name, realert_hours):
        # Ritorna (invia: bool, motivo: str, dedupe_key: str)
        from api.models import Notifica
        prefix = dedupe_prefix(monitoraggio, utente, platform_name)
        key = build_dedupe_key(monitoraggio, utente, platform_name, realert_hours)
        now = timezone.now()
        # backoff FAILED: evita tempeste di retry con SMTP rotto
        recent_failed = Notifica.objects.filter(
            dedupe_key__startswith=prefix, status="FAILED",
        ).order_by("-id").first()
        if recent_failed is not None:
            failed_at = getattr(recent_failed, "sent_at", None)
            if failed_at is None:
                failed_at = getattr(recent_failed, "creato_il", None)
            if failed_at and failed_at > now - timedelta(minutes=FAILED_BACKOFF_MINUTES):
                return False, "failed_backoff", key
        if realert_hours and realert_hours > 0:
            exists = Notifica.objects.filter(
                dedupe_key__startswith=prefix, status="SENT",
                sent_at__gte=now - timedelta(hours=realert_hours)).exists()
            if exists:
                return False, "cooldown", key
            return True, "ok", key
        # legacy: dedupe giornaliero esatto
        if Notifica.objects.filter(dedupe_key=key, status="SENT").exists():
            return False, "dedup_daily", key
        return True, "ok", key

    # --------------------------------------------------- fase 6: notifica ----
    def notify(self, monitoraggio, utente, subject, message, dedupe_key,
               email_retries, email_wait, counters):
        from api.models import Notifica
        if not getattr(utente, "notify_email", True):
            return "skip_pref"
        to_email = getattr(utente, "email", None)
        if not to_email:
            return "skip_no_email"
        ok, err = send_email_with_retry(subject, message, to_email,
                                        email_retries, email_wait)
        with transaction.atomic():
            if ok:
                Notifica.objects.create(monitoraggio=monitoraggio, channel="email",
                                        dedupe_key=dedupe_key, status="SENT",
                                        sent_at=timezone.now(), message=message)
                counters["notified"] += 1
                self.stdout.write(self.style.SUCCESS(
                    "[EMAIL SENT] monitoraggio=%s to=%s dedupe=%s" % (
                        monitoraggio.id, to_email, dedupe_key)))
                return "sent"
            Notifica.objects.create(monitoraggio=monitoraggio, channel="email",
                                    dedupe_key=dedupe_key, status="FAILED",
                                    message="%s\n\nERRORE:\n%s" % (message, err))
            counters["email_fail"] += 1
            self.stdout.write(self.style.ERROR(
                "[EMAIL FAIL] monitoraggio=%s to=%s err=%s" % (
                    monitoraggio.id, to_email, err)))
            return "failed"

    # ------------------------------------------------------------- handle ----
    def handle(self, *args, **options):
        limit = options["limit"]
        sleep_seconds = options["sleep"]
        verbose = options["verbose"]
        dry_run = options["dry_run"]
        force_available_platform = (options.get("force_available_platform") or "").strip().lower() or None
        email_retries = max(1, int(options.get("email_retries") or 3))
        email_wait = max(0.5, float(options.get("email_wait") or 1.5))
        skip_scan_hours = max(0, int(options.get("skip_scan_hours") or 0))
        include_past = bool(options.get("include_past", False))
        only_new_minutes = int(options.get("only_new_minutes") or 0)
        realert_hours = int(options.get("realert_hours") or 0)
        if only_new_minutes > 0:
            skip_scan_hours = 0  # first-scan: verifica immediata, rate-limit off

        now = timezone.now()
        self.stdout.write(self.style.SUCCESS("[START] scan_pro_alerts_generic"))
        self.stdout.write("[TIME] %s" % now.isoformat())
        self.stdout.write(
            "[CONFIG] limit=%s sleep=%s dry_run=%s skip_scan_hours=%s include_past=%s"
            % (limit, sleep_seconds, dry_run, skip_scan_hours, include_past))
        if only_new_minutes > 0:
            self.stdout.write("[CONFIG] FIRST-SCAN mode: only-new-minutes=%s" % only_new_minutes)
        if realert_hours > 0:
            self.stdout.write("[CONFIG] RE-ALERT mode: cooldown=%sh" % realert_hours)

        counters = {k: 0 for k in (
            "processed", "links_found", "ticketmaster", "ticketone", "fansale",
            "vivaticket", "other", "notified", "deduped", "unknown",
            "unavailable", "email_fail", "skipped_not_ready",
            "skipped_rate_limit", "skipped_past", "skipped_no_target",
            "skipped_no_platform", "skipped_expired", "skipped_stale",
            "failed_backoff")}

        monitoraggi = self.select_monitoraggi(options, counters)
        self.stdout.write("[PRO] monitoraggi PRO attivi trovati: %s" % len(monitoraggi))

        scan_result_cache = {}

        for monitoraggio in monitoraggi:
            counters["processed"] += 1
            abbonamento = monitoraggio.abbonamento
            utente = abbonamento.utente
            performance = monitoraggio.performance
            evento = monitoraggio.evento
            if performance and not evento:
                evento = performance.evento
            if not performance and not evento:
                counters["skipped_no_target"] += 1
                if verbose:
                    self.stdout.write(self.style.WARNING(
                        "[SKIP] monitoraggio=%s: nessun evento/performance" % monitoraggio.id))
                continue
            if performance and not include_past and _performance_is_past(performance):
                counters["skipped_past"] += 1
                if verbose:
                    self.stdout.write(self.style.WARNING(
                        "[SKIP PAST] monitoraggio=%s perf=%s: evento già passato"
                        % (monitoraggio.id, performance.id)))
                continue

            if verbose:
                self.stdout.write("")
                self.stdout.write("[MONITORAGGIO] id=%s email=%s performance_id=%s evento_id=%s"
                                  % (monitoraggio.id, utente.email,
                                     performance.id if performance else None,
                                     evento.id if evento else None))
                if evento:
                    self.stdout.write("[EVENTO] %s" % evento.nome_evento)
                if performance:
                    self.stdout.write("[PERFORMANCE] id=%s data=%s luogo=%s"
                                      % (performance.id, performance.starts_at_utc,
                                         performance.luogo.nome if performance.luogo else "-"))

            available_this_mon = []
            platform_links = self.collect_links(monitoraggio, performance, evento)
            if not platform_links:
                counters["skipped_no_platform"] += 1
                if verbose:
                    self.stdout.write(self.style.WARNING(
                        "[SKIP] monitoraggio=%s: nessuna piattaforma collegata" % monitoraggio.id))
                continue

            for link in platform_links:
                counters["links_found"] += 1
                platform_name = link["platform_name"]
                url = link["url"]
                mapping_pk = link["mapping_pk"]
                mapping_type = link["mapping_type"]
                counters[platform_name if platform_name in
                         ("ticketmaster", "ticketone", "fansale", "vivaticket") else "other"] += 1

                if not url:
                    if verbose:
                        self.stdout.write(self.style.WARNING(
                            "[SKIP URL] monitoraggio=%s platform=%s: url vuoto"
                            % (monitoraggio.id, platform_name)))
                    continue
                if platform_name in _NOT_READY_PLATFORMS:
                    counters["skipped_not_ready"] += 1
                    continue

                # rate-limit: mai per vivaticket (zero HTTP, e il suo timestamp
                # e' l'eta' del dato scritta dal refresh, non va interpretata
                # come "gia' controllato")
                cache_key = (mapping_type, mapping_pk)
                use_rate_limit = (skip_scan_hours > 0 and platform_name != "vivaticket")
                if use_rate_limit and _was_scanned_recently(mapping_type, mapping_pk, skip_scan_hours):
                    if cache_key in scan_result_cache:
                        result = scan_result_cache[cache_key]
                        if verbose:
                            self.stdout.write("[CACHE HIT] platform=%s monitoraggio=%s"
                                              % (platform_name, monitoraggio.id))
                    else:
                        counters["skipped_rate_limit"] += 1
                        if verbose:
                            self.stdout.write("[SKIP RATE] platform=%s monitoraggio=%s: già scansionato nelle ultime %sh"
                                              % (platform_name, monitoraggio.id, skip_scan_hours))
                        continue
                else:
                    if verbose:
                        self.stdout.write("[LINK] source=%s platform=%s url=%s"
                                          % (link["source"], platform_name, url))
                    result = check_platform_availability(
                        platform_name=platform_name, url=url, verbose=verbose,
                        mapping_pk=mapping_pk, mapping_type=mapping_type)
                    if platform_name != "vivaticket":
                        _touch_last_scan(mapping_type, mapping_pk)
                    scan_result_cache[cache_key] = result

                if force_available_platform and platform_name == force_available_platform:
                    result = dict(result)
                    result.update({"ok": True, "availability": "available",
                                   "reason": "FORCE_AVAILABLE"})
                    self.stdout.write(self.style.WARNING(
                        "[FORCE AVAILABLE] platform=%s monitoraggio=%s"
                        % (platform_name, monitoraggio.id)))

                self.stdout.write("[RESULT] platform=%s availability=%s reason=%s status_code=%s"
                                  % (platform_name, result["availability"],
                                     result["reason"], result.get("status_code")))

                if result.get("status_code") == 401:
                    counters["unknown"] += 1
                    if sleep_seconds > 0:
                        time.sleep(sleep_seconds)
                    continue

                availability = result["availability"]
                if availability == "available":
                    invia, motivo, dedupe_key = self.decide_alert(
                        monitoraggio, utente, platform_name, realert_hours)
                    if not invia:
                        if motivo == "failed_backoff":
                            counters["failed_backoff"] += 1
                        else:
                            counters["deduped"] += 1
                        self.stdout.write(self.style.WARNING(
                            "[%s] monitoraggio=%s platform=%s dedupe=%s"
                            % (motivo.upper(), monitoraggio.id, platform_name, dedupe_key)))
                        if sleep_seconds > 0:
                            time.sleep(sleep_seconds)
                        continue
                    email_url = result.get("final_url") or url
                    available_this_mon.append({
                        "platform_name": platform_name, "dedupe_key": dedupe_key,
                        "email_url": email_url, "result": result})
                elif availability == "unavailable":
                    counters["unavailable"] += 1
                else:
                    if "snapshot_stale" in (result.get("reason") or ""):
                        counters["skipped_stale"] += 1
                    counters["unknown"] += 1

                if sleep_seconds > 0:
                    time.sleep(sleep_seconds)

            if available_this_mon:
                subject, message = build_multi_email_message(
                    utente, monitoraggio, performance, evento, available_this_mon)
                plats = [a["platform_name"] for a in available_this_mon]
                if dry_run:
                    self.stdout.write(self.style.WARNING(
                        "[DRY-RUN EMAIL] to=%s platforms=%s subject=%s"
                        % (utente.email, plats, subject)))
                    counters["notified"] += 1
                else:
                    # una email sola; una Notifica per piattaforma (dedupe per-piattaforma)
                    from api.models import Notifica as _Notifica
                    ok, err = send_email_with_retry(subject, message, utente.email,
                                                    email_retries, email_wait)
                    with transaction.atomic():
                        for a in available_this_mon:
                            _Notifica.objects.create(
                                monitoraggio=monitoraggio, channel="email",
                                dedupe_key=a["dedupe_key"],
                                status="SENT" if ok else "FAILED",
                                sent_at=timezone.now() if ok else None,
                                message=message if ok else message + "\n\nERRORE:\n" + str(err))
                    if ok:
                        counters["notified"] += 1
                        self.stdout.write(self.style.SUCCESS(
                            "[EMAIL SENT] monitoraggio=%s to=%s platforms=%s"
                            % (monitoraggio.id, utente.email, plats)))
                    else:
                        counters["email_fail"] += 1
                        self.stdout.write(self.style.ERROR(
                            "[EMAIL FAIL] monitoraggio=%s err=%s" % (monitoraggio.id, err)))

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("[DONE]"))
        order = [("processed", "processed"), ("links_found", "links_found"),
                 ("ticketmaster", "  ticketmaster_links"), ("ticketone", "  ticketone_links"),
                 ("fansale", "  fansale_links"), ("vivaticket", "  vivaticket_links"),
                 ("other", "  other_links"), (None, ""), ("notified", "notified"),
                 ("deduped", "deduped"), ("unknown", "unknown"),
                 ("unavailable", "unavailable"), ("email_fail", "email_fail"),
                 ("failed_backoff", "failed_backoff"), ("skipped_not_ready", "skipped_not_ready"),
                 ("skipped_rate_limit", "skipped_rate_limit"), ("skipped_past", "skipped_past"),
                 ("skipped_stale", "skipped_stale"), ("skipped_expired", "skipped_expired"),
                 ("skipped_no_target", "skipped_no_target"),
                 ("skipped_no_platform", "skipped_no_platform")]
        for key, label in order:
            if key is None:
                self.stdout.write("")
            else:
                self.stdout.write("%-21s=%s" % (label, counters[key]))
