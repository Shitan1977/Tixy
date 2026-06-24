"""
scan_vivaticket_pro.py
======================
Django management command — Mister Alert / Tixy

Scanner PRO Vivaticket basato sui dati snapshot_raw + Performance.
NON fa HTTP verso shop.vivaticket.com (bloccato da Incapsula/Imperva).
Determina lo stato dai dati già acquisiti dall'importer e rileva transizioni.
Invia email PRO agli utenti monitoranti quando un evento torna disponibile.

Uso:
    python manage.py scan_vivaticket_pro
    python manage.py scan_vivaticket_pro --limit 20
    python manage.py scan_vivaticket_pro --only-id 1671 --verbose
    python manage.py scan_vivaticket_pro --dry-run --verbose
    python manage.py scan_vivaticket_pro --alert-on-transition --verbose
    python manage.py scan_vivaticket_pro --alert-on-transition --only-email user@example.com

Opzioni:
    --limit                Max PerformancePiattaforma da processare (default 100)
    --only-id              Processa solo il record con questo id (debug)
    --dry-run              Non salva nulla sul DB, non invia email
    --verbose              Log esteso (tutti i campi rilevanti)
    --alert-on-transition  Attiva notifica PRO su transizione → available
    --reset-status         Resetta last_vivaticket_status a None (utile per re-test)
    --email-retries        Tentativi invio email in caso di errore (default 3)
    --email-wait           Secondi di attesa tra retry (default 1.5)
    --only-email           Invia solo all'email indicata (test controllati)

Logica stati
------------
Fonte primaria:  snapshot_raw["sale_status"] + snapshot_raw["is_sell_active"]
Fonte secondaria: Performance.status + Performance.disponibilita_agg
Fonte terziaria:  snapshot_raw["performance_status"] (codice numerico Vivaticket)

Mappa stati Vivaticket:
  performance_status in PERF_STATUS_HARD_UNAVAILABLE  (cancellato/rimosso)
    → unavailable  [hard block — vince su tutto]

  sale_status in SALE_STATUS_SOLDOUT_VALUES
    → sold_out

  is_sell_active == False
    → unavailable  (salvo incongruenza con disponibilita_agg → unknown)

  sale_status in SALE_STATUS_AVAILABLE_VALUES e is_sell_active == True
    → available  (salvo disponibilita_agg=soldout → sold_out)

  sale_status in SALE_STATUS_SPECIAL_VALUES e is_sell_active == True
  e almeno un segnale positivo (disponibilita_agg=disponibile o Performance.status=ONSALE)
    → available  (caso "available_or_special", performance_status=102, ecc.)

  performance_status sconosciuto (non in nessuna lista) e is_sell_active == True
    → unknown  (codice nuovo non mappato — non bloccare, non confermare)

  Fallback su Performance.disponibilita_agg / Performance.status
    → available / sold_out

  Tutto il resto
    → unknown

Logica alert email
------------------
  1. Solo se --alert-on-transition attivo
  2. Solo se previous_status in {sold_out, unavailable, unknown} (NON None, NON available)
  3. Solo se new_status == "available"
  4. Cerca Monitoraggio.objects.filter(performance=perf, abbonamento PRO attivo)
  5. Salta se esistono Biglietto valido o Listing ACTIVE per quella Performance
  6. Deduplica giornaliera: vivaticket:AVAILABLE:perf:{id}:user:{id}:{YYYY-MM-DD}
  7. Rispetta user.notify_email
  8. Salva Notifica con status SENT o FAILED
  9. dry-run: solo log, nessuna email, nessun salvataggio
"""

import logging
import time

from django.conf import settings
from django.core.mail import send_mail
from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from api.models import (
    Biglietto,
    Listing,
    Monitoraggio,
    Notifica,
    PerformancePiattaforma,
)

logger = logging.getLogger(__name__)


# ── Costanti stati ────────────────────────────────────────────────────────────

STATUS_AVAILABLE   = "available"
STATUS_SOLD_OUT    = "sold_out"
STATUS_UNAVAILABLE = "unavailable"
STATUS_UNKNOWN     = "unknown"

ALL_STATUSES = {STATUS_AVAILABLE, STATUS_SOLD_OUT, STATUS_UNAVAILABLE, STATUS_UNKNOWN}

# Stati da cui una transizione a "available" è considerata un alert.
# None è escluso deliberatamente: il primo scan non genera mai alert.
ALERT_TRIGGER_PREVIOUS_STATUSES = {STATUS_SOLD_OUT, STATUS_UNAVAILABLE, STATUS_UNKNOWN}

# ── Costanti sale_status ──────────────────────────────────────────────────────

# Disponibilità piena e confermata
SALE_STATUS_AVAILABLE_VALUES = {"available", "on_sale", "onsale", "in_vendita"}

# Disponibilità speciale o condizionata (presale, vip, ecc.)
# Trattati come available solo se confermati da segnale secondario.
SALE_STATUS_SPECIAL_VALUES = {
    "available_or_special",
    "presale",
    "pre_sale",
    "special",
    "special_sale",
    "member_only",
    "vip",
    "vip_only",
    "fan_presale",
    "waitlist",
}

# Sold out o non disponibilità esplicita
SALE_STATUS_SOLDOUT_VALUES = {
    "sold_out", "soldout", "esaurito",
    "unavailable", "not_available",
    "cancelled", "canceled", "annullato",
    "postponed",
}

# ── Costanti performance_status numerico ──────────────────────────────────────
#
# REGOLA: aggiungere a PERF_STATUS_HARD_UNAVAILABLE SOLO codici documentati
# con certezza come "evento cancellato definitivamente".
# Codici sconosciuti NON bloccano: possono essere nuove varianti attive.
#
# 100  = attivo/normale
# 102  = variante attiva (es. prevendita speciale)
# 110  = rinviato ma esistente
# 200  = chiuso/terminato (evento passato)
# 201  = annullato con rimborso in corso
PERF_STATUS_ACTIVE           = {100, 102, 110}
PERF_STATUS_CLOSED           = {200, 201}
PERF_STATUS_HARD_UNAVAILABLE = set()   # espandere solo con certezza documentata

# ── Costanti Performance model ────────────────────────────────────────────────

PERFORMANCE_STATUS_AVAILABLE = {"onsale", "on_sale", "available"}
DISPONIBILITA_AVAILABLE      = {"disponibile", "available", "in_vendita"}
DISPONIBILITA_SOLDOUT        = {"esaurito", "sold_out", "soldout", "non_disponibile", "non disponibile"}

# ── Costanti notifiche ────────────────────────────────────────────────────────

ALERT_REASON   = "AVAILABLE"
ALERT_PLATFORM = "vivaticket"


# ── Logica rilevamento stato ──────────────────────────────────────────────────

# ── Derivazione automatica campi shop dai button ──────────────────────────────
import re as _re_vt


def derive_shop_fields(snapshot):
    """Deriva shop_type/pcode/tcode/is_sell_active dai button se mancanti."""
    snap = dict(snapshot or {})

    if "is_sell_active" not in snap:
        sell_active = snap.get("sell_active")
        if sell_active is True:
            snap["is_sell_active"] = True
        elif sell_active is False:
            snap["is_sell_active"] = False

    need_shop = (
        snap.get("shop_type") != "vivaticket_shop"
        or not snap.get("pcode")
        or not snap.get("tcode")
    )
    if need_shop:
        buttons = snap.get("buttons", []) or []
        sell_btn = None
        for b in buttons:
            u = b.get("url", "") or ""
            if b.get("type") == "sell" and "shop.vivaticket.com" in u:
                sell_btn = b
                break
        if sell_btn:
            sell_url = sell_btn["url"].replace("&amp;", "&")
            m_pcode = _re_vt.search(r"pcode=(\d+)", sell_url)
            m_tcode = _re_vt.search(r"tcode=([A-Za-z0-9]+)", sell_url)
            if m_pcode and m_tcode:
                snap["shop_type"] = "vivaticket_shop"
                snap["pcode"] = m_pcode.group(1)
                snap["tcode"] = m_tcode.group(1)
                if "is_sell_active" not in snap and sell_btn.get("active") is True:
                    snap["is_sell_active"] = True

    return snap


def detect_status_from_snapshot(snapshot: dict, performance=None) -> tuple[str, str]:
    """
    Determina lo stato di disponibilità di un evento Vivaticket
    a partire dai dati già acquisiti dall'importer.

    Non fa chiamate HTTP. Usa solo:
      - snapshot_raw (sale_status, is_sell_active, performance_status)
      - Performance.status
      - Performance.disponibilita_agg

    Ritorna:
      (status, reason)
      dove status è uno di: available / sold_out / unavailable / unknown
      e reason è una stringa leggibile che spiega perché.

    Principio guida:
      - I segnali "negativi certi" bloccano presto e forte.
      - I codici performance_status sconosciuti NON bloccano.
      - I sale_status "speciali" richiedono conferma secondaria.
      - In caso di incongruenza si preferisce unknown a unavailable.

    Logica a livelli:
      0 — hard block performance_status certi di cancellazione
      1 — sold_out esplicito da sale_status
      2 — is_sell_active == False
      3 — sale_status standard available + is_sell_active=True
      4 — sale_status speciale + is_sell_active=True (con/senza conferma)
      5 — performance_status sconosciuto (pass-through ai livelli successivi)
      6 — fallback su Performance.disponibilita_agg
      7 — fallback su Performance.status
      8 — unknown
    """

    sale_status_raw    = snapshot.get("sale_status", "")
    sale_status        = (sale_status_raw or "").lower().strip()
    is_sell_active     = snapshot.get("is_sell_active")
    performance_status = snapshot.get("performance_status")

    # Normalizza performance_status a intero
    ps: int | None = None
    if performance_status is not None:
        try:
            ps = int(performance_status)
        except (TypeError, ValueError):
            ps = None

    # Campi Performance
    perf_status_raw        = None
    perf_disponibilita_raw = None
    if performance is not None:
        perf_status_raw        = getattr(performance, "status", None)
        perf_disponibilita_raw = getattr(performance, "disponibilita_agg", None)

    perf_status        = (perf_status_raw        or "").lower().strip()
    perf_disponibilita = (perf_disponibilita_raw or "").lower().strip()

    # Almeno un segnale secondario positivo
    has_positive_secondary = (
        perf_disponibilita in DISPONIBILITA_AVAILABLE
        or perf_status in PERFORMANCE_STATUS_AVAILABLE
    )

    # ── Livello 0: hard block ps certi ───────────────────────────────────
    if ps is not None and ps in PERF_STATUS_HARD_UNAVAILABLE:
        return (
            STATUS_UNAVAILABLE,
            f"performance_status={ps} in PERF_STATUS_HARD_UNAVAILABLE (cancellato)"
        )

    # ── Livello 1: sold_out esplicito ─────────────────────────────────────
    if sale_status in SALE_STATUS_SOLDOUT_VALUES:
        return STATUS_SOLD_OUT, f"sale_status='{sale_status_raw}'"

    # ── Livello 2: vendita disattivata ────────────────────────────────────
    if is_sell_active is False:
        if perf_disponibilita in DISPONIBILITA_AVAILABLE:
            return (
                STATUS_UNKNOWN,
                f"is_sell_active=False ma disponibilita_agg='{perf_disponibilita_raw}' "
                f"→ dati incongruenti, verifica importer"
            )
        return (
            STATUS_UNAVAILABLE,
            f"is_sell_active=False sale_status='{sale_status_raw}'"
        )

    # ── Livello 3: sale_status standard available ─────────────────────────
    if sale_status in SALE_STATUS_AVAILABLE_VALUES and is_sell_active is True:
        if perf_disponibilita in DISPONIBILITA_SOLDOUT:
            return (
                STATUS_SOLD_OUT,
                f"sale_status='{sale_status_raw}' ma disponibilita_agg='{perf_disponibilita_raw}' "
                f"→ sold_out prevalente (Performance più recente)"
            )
        return (
            STATUS_AVAILABLE,
            f"sale_status='{sale_status_raw}' is_sell_active=True"
            + (f" performance_status={ps}" if ps is not None else "")
        )

    # ── Livello 4: sale_status speciale ───────────────────────────────────
    if sale_status in SALE_STATUS_SPECIAL_VALUES and is_sell_active is True:
        if perf_disponibilita in DISPONIBILITA_SOLDOUT:
            return (
                STATUS_SOLD_OUT,
                f"sale_status='{sale_status_raw}' (speciale) ma disponibilita_agg='{perf_disponibilita_raw}'"
            )
        if has_positive_secondary:
            return (
                STATUS_AVAILABLE,
                f"sale_status='{sale_status_raw}' (speciale) confermato da "
                f"disponibilita_agg='{perf_disponibilita_raw}' "
                f"Performance.status='{perf_status_raw}'"
                + (f" performance_status={ps}" if ps is not None else "")
            )
        return (
            STATUS_UNKNOWN,
            f"sale_status='{sale_status_raw}' (speciale) senza conferma secondaria"
            + (f" performance_status={ps}" if ps is not None else "")
        )

    # ── Livello 5: ps sconosciuto — pass-through ──────────────────────────
    # Nessun blocco. I livelli successivi decidono.

    # ── Livello 6: fallback Performance.disponibilita_agg ────────────────
    if perf_disponibilita in DISPONIBILITA_AVAILABLE:
        return (
            STATUS_AVAILABLE,
            f"disponibilita_agg='{perf_disponibilita_raw}' "
            f"(sale_status='{sale_status_raw}'"
            + (f" performance_status={ps}" if ps is not None else "")
            + ")"
        )
    if perf_disponibilita in DISPONIBILITA_SOLDOUT:
        return STATUS_SOLD_OUT, f"disponibilita_agg='{perf_disponibilita_raw}'"

    # ── Livello 7: fallback Performance.status ────────────────────────────
    if perf_status in PERFORMANCE_STATUS_AVAILABLE:
        return (
            STATUS_AVAILABLE,
            f"Performance.status='{perf_status_raw}' (dati parziali)"
            + (f" performance_status={ps}" if ps is not None else "")
        )

    # ── Livello 8: nessun segnale affidabile ──────────────────────────────
    return (
        STATUS_UNKNOWN,
        f"sale_status='{sale_status_raw}' is_sell_active={is_sell_active} "
        f"performance_status={ps} "
        f"perf_status='{perf_status_raw}' disponibilita='{perf_disponibilita_raw}'"
    )


def is_transition_to_available(previous_status: str | None, new_status: str) -> bool:
    """
    True solo se lo stato passa da un valore negativo noto a "available".

    previous_status == None  →  False (primo scan, nessun alert per evitare
                                       di notificare eventi sempre disponibili)
    previous_status == "available"  →  False (nessun cambiamento)
    """
    if previous_status is None:
        return False
    return (
        previous_status in ALERT_TRIGGER_PREVIOUS_STATUSES
        and new_status == STATUS_AVAILABLE
    )


# ── Helpers info evento ───────────────────────────────────────────────────────

def _get_title(pp) -> str:
    """Recupera il titolo dell'evento dal grafo PP → snapshot → Performance → Evento."""
    try:
        title = (pp.snapshot_raw or {}).get("title", "")
        if title:
            return title
        perf = pp.performance
        if perf is None:
            return f"pp_id={pp.id}"
        if hasattr(perf, "evento") and perf.evento:
            return getattr(perf.evento, "nome_evento", None) or f"evento_id={perf.evento_id}"
        return f"performance_id={perf.id}"
    except Exception:
        return f"pp_id={pp.id}"


def _get_event_info(pp) -> dict:
    """Raccoglie info evento per log e corpo email da snapshot_raw + Performance."""
    snap = pp.snapshot_raw or {}
    perf = pp.performance

    city  = snap.get("city", "-")
    venue = snap.get("venue", "-")
    date  = snap.get("raw_date") or snap.get("starts_at_raw", "-")

    # Preferisci dati Performance se più completi
    if perf:
        if hasattr(perf, "luogo") and perf.luogo:
            venue = perf.luogo.nome or venue
            city = getattr(perf.luogo, "citta", None) or city
        if hasattr(perf, "starts_at_utc") and perf.starts_at_utc:
            date = perf.starts_at_utc.strftime("%d/%m/%Y %H:%M")

    return {
        "title":      snap.get("title", "-"),
        "city":       city,
        "venue":      venue,
        "date":       date,
        "pcode":      snap.get("pcode", "-"),
        "tcode":      snap.get("tcode", "-"),
        "shop_url":   snap.get("shop_url", "-"),
        "source_url": snap.get("source_url", "-"),
    }


# ── Helpers abbonamento ───────────────────────────────────────────────────────

def _abbonamento_is_active(ab) -> bool:
    """True se l'abbonamento è attivo e non scaduto."""
    if not getattr(ab, "attivo", False):
        return False
    data_fine = getattr(ab, "data_fine", None)
    if data_fine and data_fine < timezone.now():
        return False
    return True


def _has_internal_tickets(perf) -> bool:
    """
    True se esistono già biglietti interni validi o listing attivi per questa
    Performance. In quel caso l'alert Vivaticket non serve.
    """
    if Biglietto.objects.filter(performance=perf, is_valid=True).exists():
        return True
    if Listing.objects.filter(performance=perf, status="ACTIVE").exists():
        return True
    return False


# ── Deduplica ─────────────────────────────────────────────────────────────────

def _dedupe_key(perf_id: int, user_id: int, platform: str, reason: str) -> str:
    """
    Chiave di deduplica giornaliera.
    Formato: vivaticket:AVAILABLE:perf:{perf_id}:user:{user_id}:{YYYY-MM-DD}
    """
    day = timezone.now().date().isoformat()
    return f"{platform}:{reason}:perf:{perf_id}:user:{user_id}:{day}"


# ── Invio email ───────────────────────────────────────────────────────────────

def _build_email(info: dict, perf_id: int) -> tuple[str, str]:
    """
    Costruisce subject e corpo dell'email alert Vivaticket.
    Ritorna (subject, message).
    """
    subject = f"[Tixy] Biglietti disponibili su Vivaticket - {info['title']}"

    link = info["shop_url"] if info["shop_url"] != "-" else info["source_url"]

    message = (
        f"Ciao,\n\n"
        f"abbiamo trovato un aggiornamento per il tuo monitoraggio PRO su Vivaticket.\n\n"
        f"Evento:  {info['title']}\n"
        f"Luogo:   {info['venue']} - {info['city']}\n"
        f"Data:    {info['date']}\n"
        f"Stato:   Biglietti disponibili\n"
        f"Link Vivaticket: {link}\n\n"
        f"Grazie,\n"
        f"Tixy"
    )
    return subject, message


def _send_email_with_retry(
    *,
    subject: str,
    message: str,
    to_email: str,
    max_retries: int,
    base_wait: float,
) -> tuple[bool, str]:
    """
    Invia email con retry esponenziale.
    Ritorna (ok: bool, error_message: str).
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
        except Exception as exc:
            last_err = str(exc)
            wait = base_wait * attempt
            logger.warning(
                "Email attempt %d/%d failed for %s: %s — retry in %.1fs",
                attempt, max_retries, to_email, last_err, wait,
            )
            time.sleep(wait)
    return False, last_err


# ── Alert PRO ─────────────────────────────────────────────────────────────────

def trigger_alert(
    pp,
    new_status: str,
    previous_status: str,
    reason: str,
    dry_run: bool,
    email_retries: int = 3,
    email_wait: float = 1.5,
    only_email: str | None = None,
    stdout=None,
    counters: dict | None = None,
) -> None:
    """
    Invia email PRO agli utenti che monitorano questa Performance su Vivaticket.

    Flusso:
      1. Recupera la Performance collegata al pp.
      2. Salta se esistono biglietti interni / listing attivi.
      3. Cerca i Monitoraggio PRO attivi collegati alla Performance.
      4. Per ogni monitoraggio:
         a. Verifica abbonamento PRO attivo.
         b. Verifica user.notify_email.
         c. Controlla deduplica giornaliera.
         d. Invia email (o simula in dry-run).
         e. Salva Notifica SENT/FAILED.

    Args:
        pp:            PerformancePiattaforma instance
        new_status:    nuovo stato ("available")
        previous_status: stato precedente
        reason:        stringa diagnostica dal detector
        dry_run:       se True, nessuna email e nessun salvataggio
        email_retries: tentativi invio email
        email_wait:    secondi base tra retry
        only_email:    se valorizzato, invia solo a questo indirizzo (test)
        stdout:        BaseCommand.stdout per log (opzionale)
        counters:      dict contatori da aggiornare in-place (opzionale)
    """

    def _log(msg: str) -> None:
        if stdout:
            stdout.write(msg)
        else:
            logger.info(msg)

    if counters is None:
        counters = {}

    def _inc(key: str) -> None:
        counters[key] = counters.get(key, 0) + 1

    performance = pp.performance
    if performance is None:
        _log(f"  [ALERT SKIP] pp_id={pp.id} reason=no_performance")
        _inc("skip_no_monitoraggio")
        return

    perf = performance
    info = _get_event_info(pp)

    # ── Salta se esistono biglietti interni ───────────────────────────────
    if _has_internal_tickets(perf):
        _log(
            f"  [ALERT SKIP] pp_id={pp.id} perf_id={perf.id} "
            f"reason=internal_tickets_exist"
        )
        _inc("skip_internal")
        return

    # ── Cerca monitoraggi PRO attivi per questa Performance ───────────────
    now = timezone.now()
    monitoraggi_qs = (
        Monitoraggio.objects
        .filter(
            performance=perf,
            abbonamento__attivo=True,
            abbonamento__plan__plan_type="PRO",
        )
        .filter(
            Q(abbonamento__data_fine__isnull=True)
            | Q(abbonamento__data_fine__gte=now)
        )
        .select_related(
            "abbonamento",
            "abbonamento__utente",
            "abbonamento__plan",
            "performance",
        )
        .order_by("id")
    )

    if not monitoraggi_qs.exists():
        _log(
            f"  [ALERT SKIP] pp_id={pp.id} perf_id={perf.id} "
            f"reason=no_pro_monitoraggi"
        )
        _inc("skip_no_monitoraggio")
        return

    subject, message = _build_email(info, perf.id)

    # ── Loop monitoraggi ──────────────────────────────────────────────────
    for m in monitoraggi_qs:
        ab   = m.abbonamento
        user = ab.utente

        # Verifica abbonamento attivo (doppio controllo sul record reale)
        if not _abbonamento_is_active(ab):
            _log(
                f"  [ALERT SKIP] monitoraggio={m.id} user={user.id} "
                f"reason=abbonamento_not_active"
            )
            _inc("skip_inactive_abbonamento")
            continue

        # Filtro --only-email (test controllati)
        if only_email and getattr(user, "email", None) != only_email:
            continue

        # Verifica preferenza email utente
        if not getattr(user, "notify_email", True):
            _log(
                f"  [ALERT SKIP] monitoraggio={m.id} user={user.id} "
                f"reason=notify_email_disabled"
            )
            _inc("no_email_pref")
            continue

        to_email = getattr(user, "email", None)
        if not to_email:
            _log(
                f"  [ALERT SKIP] monitoraggio={m.id} user={user.id} "
                f"reason=no_email_address"
            )
            _inc("no_email_pref")
            continue

        # Deduplica giornaliera
        dedupe = _dedupe_key(perf.id, user.id, ALERT_PLATFORM, ALERT_REASON)
        if Notifica.objects.filter(dedupe_key=dedupe, status="SENT").exists():
            _log(
                f"  [ALERT SKIP] monitoraggio={m.id} user={user.id} "
                f"reason=dedup dedupe_key={dedupe}"
            )
            _inc("skip_dedup")
            continue

        # ── dry-run: solo log ─────────────────────────────────────────────
        if dry_run:
            _log(
                f"  [DRY-RUN ALERT] monitoraggio={m.id} "
                f"user={user.id} email={to_email} "
                f"perf_id={perf.id} title={info['title'][:50]} "
                f"dedupe_key={dedupe}"
            )
            _inc("notified")
            continue

        # ── Invio email ───────────────────────────────────────────────────
        ok, err = _send_email_with_retry(
            subject=subject,
            message=message,
            to_email=to_email,
            max_retries=email_retries,
            base_wait=email_wait,
        )

        # ── Salva Notifica ────────────────────────────────────────────────
        if ok:
            Notifica.objects.create(
                monitoraggio=m,
                channel="email",
                dedupe_key=dedupe,
                status="SENT",
                message=message,
            )
            _log(
                f"  [EMAIL SENT] monitoraggio={m.id} user={user.id} "
                f"email={to_email} perf_id={perf.id}"
            )
            _inc("notified")
        else:
            Notifica.objects.create(
                monitoraggio=m,
                channel="email",
                dedupe_key=dedupe,
                status="FAILED",
                message=f"{message}\n\nERRORE INVIO EMAIL:\n{err}",
            )
            _log(
                f"  [EMAIL FAIL] monitoraggio={m.id} user={user.id} "
                f"email={to_email} error={err}"
            )
            _inc("email_fail")


# ── Management command ────────────────────────────────────────────────────────

class Command(BaseCommand):
    help = (
        "Scanner PRO Vivaticket — determina disponibilità da snapshot_raw + Performance. "
        "Non fa chiamate HTTP (Incapsula). Rileva transizioni e invia email PRO."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit", type=int, default=100,
            help="Max PerformancePiattaforma da processare (default 100)",
        )
        parser.add_argument(
            "--only-id", type=int, default=None,
            help="Processa solo il record con questo id (debug)",
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Non salva nulla sul DB, non invia email",
        )
        parser.add_argument(
            "--verbose", action="store_true",
            help="Log esteso con tutti i campi rilevanti",
        )
        parser.add_argument(
            "--alert-on-transition", action="store_true", default=False,
            help="Attiva email PRO quando lo stato passa a 'available'",
        )
        parser.add_argument(
            "--reset-status", action="store_true", default=False,
            help="Resetta last_vivaticket_status a None (utile per re-test alert)",
        )
        parser.add_argument(
            "--email-retries", type=int, default=3,
            help="Tentativi invio email in caso di errore (default 3)",
        )
        parser.add_argument(
            "--email-wait", type=float, default=1.5,
            help="Secondi base di attesa tra retry email (default 1.5)",
        )
        parser.add_argument(
            "--only-email", type=str, default=None,
            help="Invia solo all'email indicata (test controllati)",
        )

    def handle(self, *args, **options):
        limit               = options["limit"]
        only_id             = options["only_id"]
        dry_run             = options["dry_run"]
        verbose             = options["verbose"]
        alert_on_transition = options["alert_on_transition"]
        reset_status        = options["reset_status"]
        email_retries       = options["email_retries"]
        email_wait          = options["email_wait"]
        only_email          = options["only_email"]

        self.stdout.write(self.style.SUCCESS("[START] scan_vivaticket_pro"))
        self.stdout.write(f"[TIME]   {timezone.now().isoformat()}")
        self.stdout.write(
            f"[CONFIG] limit={limit} dry_run={dry_run} only_id={only_id} "
            f"alert_on_transition={alert_on_transition} reset_status={reset_status}"
        )
        if alert_on_transition:
            self.stdout.write(
                f"[EMAIL]  retries={email_retries} wait={email_wait}s "
                f"only_email={only_email or '(tutti)'}"
            )
        self.stdout.write(
            "[MODE]   Source: snapshot_raw + Performance (no HTTP Vivaticket shop)"
        )
        self.stdout.write("")

        # ── Queryset ──────────────────────────────────────────────────────
        qs = (
            PerformancePiattaforma.objects
            .select_related("performance", "piattaforma")
            .filter(piattaforma__nome__iexact="vivaticket")
            .order_by("-id")
        )
        if only_id:
            qs = qs.filter(id=only_id)

        # ── Contatori ─────────────────────────────────────────────────────
        processed           = 0
        skipped             = 0
        count_available     = 0
        count_sold_out      = 0
        count_unavailable   = 0
        count_unknown       = 0
        count_unchanged     = 0
        count_transitioned  = 0
        errors              = 0

        # Contatori email (accumulati da trigger_alert via dict condiviso)
        email_counters = {
            "notified":                 0,
            "skip_dedup":               0,
            "email_fail":               0,
            "no_email_pref":            0,
            "skip_internal":            0,
            "skip_no_monitoraggio":     0,
            "skip_inactive_abbonamento": 0,
        }

        # ── Loop principale ───────────────────────────────────────────────
        for pp in qs:
            if processed >= limit:
                break

            snapshot  = pp.snapshot_raw or {}
            derived = derive_shop_fields(snapshot)
            if derived != snapshot:
                snapshot = derived
                if not dry_run:
                    pp.snapshot_raw = snapshot
                    pp.save(update_fields=["snapshot_raw"])
            shop_type = snapshot.get("shop_type")
            pcode     = snapshot.get("pcode")
            tcode     = snapshot.get("tcode")

            # ── Filtri ammissibilità ──────────────────────────────────────
            if shop_type != "vivaticket_shop":
                skipped += 1
                if verbose:
                    self.stdout.write(
                        f"[SKIP] pp_id={pp.id} reason=shop_type={shop_type!r}"
                    )
                continue

            if not pcode or not tcode:
                skipped += 1
                if verbose:
                    self.stdout.write(
                        f"[SKIP] pp_id={pp.id} reason=missing pcode={pcode} tcode={tcode}"
                    )
                continue

            processed += 1

            try:
                # ── Reset status (modalità re-test) ───────────────────────
                if reset_status and not dry_run:
                    new_snap = dict(snapshot)
                    new_snap["last_vivaticket_status"] = None
                    pp.snapshot_raw = new_snap
                    pp.save(update_fields=["snapshot_raw"])
                    snapshot = new_snap

                previous_status = snapshot.get("last_vivaticket_status")

                # ── Rilevamento stato ─────────────────────────────────────
                performance = pp.performance
                new_status, reason = detect_status_from_snapshot(snapshot, performance)

                # ── Contatori stato ───────────────────────────────────────
                if new_status == STATUS_AVAILABLE:
                    count_available += 1
                elif new_status == STATUS_SOLD_OUT:
                    count_sold_out += 1
                elif new_status == STATUS_UNAVAILABLE:
                    count_unavailable += 1
                else:
                    count_unknown += 1

                status_changed = (new_status != previous_status)
                if status_changed:
                    count_transitioned += 1
                else:
                    count_unchanged += 1

                # ── Rilevamento transizione alert ─────────────────────────
                do_alert = (
                    alert_on_transition
                    and is_transition_to_available(previous_status, new_status)
                )

                # ── Log ───────────────────────────────────────────────────
                title = _get_title(pp)

                change_marker = ""
                if status_changed:
                    change_marker = f" [CHANGED: {previous_status} → {new_status}]"
                if do_alert:
                    change_marker += " *** ALERT ***"

                self.stdout.write(
                    f"[CHECK] pp_id={pp.id:>6} "
                    f"status={new_status:<12} "
                    f"prev={str(previous_status):<12} "
                    f"title={title[:60]}"
                    f"{change_marker}"
                )

                if verbose:
                    info = _get_event_info(pp)
                    self.stdout.write(
                        f"         sale_status={snapshot.get('sale_status')!r} "
                        f"is_sell_active={snapshot.get('is_sell_active')} "
                        f"performance_status={snapshot.get('performance_status')}"
                    )
                    if performance:
                        self.stdout.write(
                            f"         Performance.status="
                            f"{getattr(performance, 'status', None)!r} "
                            f"disponibilita_agg="
                            f"{getattr(performance, 'disponibilita_agg', None)!r} "
                            f"prezzo_min={getattr(performance, 'prezzo_min', None)} "
                            f"prezzo_max={getattr(performance, 'prezzo_max', None)}"
                        )
                    self.stdout.write(f"         reason: {reason}")
                    self.stdout.write(
                        f"         city={info['city']} date={info['date']}"
                    )
                    self.stdout.write(f"         shop_url={info['shop_url']}")

                # ── Alert PRO ─────────────────────────────────────────────
                if do_alert:
                    self.stdout.write(self.style.WARNING(
                        f"[ALERT] pp_id={pp.id} | {previous_status} → {new_status} | "
                        f"{title[:60]} | reason: {reason}"
                    ))
                    try:
                        trigger_alert(
                            pp=pp,
                            new_status=new_status,
                            previous_status=previous_status,
                            reason=reason,
                            dry_run=dry_run,
                            email_retries=email_retries,
                            email_wait=email_wait,
                            only_email=only_email,
                            stdout=self.stdout,
                            counters=email_counters,
                        )
                    except Exception as exc:
                        self.stdout.write(self.style.ERROR(
                            f"[ALERT ERROR] pp_id={pp.id} error={exc}"
                        ))
                        logger.exception(
                            "trigger_alert error pp_id=%s", pp.id
                        )

                # ── Salvataggio DB ────────────────────────────────────────
                if not dry_run:
                    new_snapshot = dict(snapshot)
                    new_snapshot["last_scan_vivaticket_pro"]    = timezone.now().isoformat()
                    new_snapshot["last_vivaticket_status"]      = new_status
                    new_snapshot["last_vivaticket_reason"]      = reason
                    new_snapshot["last_vivaticket_prev_status"] = previous_status

                    pp.snapshot_raw = new_snapshot
                    pp.save(update_fields=["snapshot_raw"])

            except Exception as exc:
                errors += 1
                self.stdout.write(self.style.ERROR(
                    f"[ERROR] pp_id={pp.id} error={exc}"
                ))
                logger.exception("scan_vivaticket_pro error pp_id=%s", pp.id)

        # ── Riepilogo finale ──────────────────────────────────────────────
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("[END] scan_vivaticket_pro"))
        self.stdout.write(
            f"processed={processed} skipped={skipped} errors={errors}"
        )
        self.stdout.write(
            f"available={count_available} sold_out={count_sold_out} "
            f"unavailable={count_unavailable} unknown={count_unknown}"
        )
        self.stdout.write(
            f"status_changed={count_transitioned} unchanged={count_unchanged}"
        )

        if alert_on_transition:
            self.stdout.write(
                f"notified={email_counters['notified']} "
                f"email_fail={email_counters['email_fail']} "
                f"skip_dedup={email_counters['skip_dedup']} "
                f"no_email_pref={email_counters['no_email_pref']} "
                f"skip_internal={email_counters['skip_internal']} "
                f"skip_no_monitoraggio={email_counters['skip_no_monitoraggio']} "
                f"skip_inactive_abbonamento="
                f"{email_counters['skip_inactive_abbonamento']}"
            )