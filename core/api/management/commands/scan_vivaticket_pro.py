"""
scan_vivaticket_pro.py
======================
Django management command — Mister Alert / Tixy

Scanner PRO Vivaticket basato sui dati snapshot_raw + Performance.
NON fa HTTP verso shop.vivaticket.com (bloccato da Incapsula/Imperva).
Determina lo stato dai dati già acquisiti dall'importer e rileva transizioni.

Uso:
    python manage.py scan_vivaticket_pro
    python manage.py scan_vivaticket_pro --limit 20
    python manage.py scan_vivaticket_pro --only-id 1671 --verbose
    python manage.py scan_vivaticket_pro --dry-run --verbose
    python manage.py scan_vivaticket_pro --alert-on-transition --verbose

Opzioni:
    --limit              Max PerformancePiattaforma da processare (default 100)
    --only-id            Processa solo il record con questo id (debug)
    --dry-run            Non salva nulla sul DB, non invia alert
    --verbose            Log esteso (tutti i campi rilevanti)
    --alert-on-transition  Attiva notifica PRO su transizione → available
    --reset-status       Resetta last_vivaticket_status a None (utile per re-test)

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
  e almeno un segnale positivo
    → unknown  (codice nuovo non mappato — non bloccare, non confermare)

  Fallback su Performance.disponibilita_agg / Performance.status
    → available / sold_out

  Tutto il resto
    → unknown
"""

import logging

from django.core.management.base import BaseCommand
from django.utils import timezone

from api.models import PerformancePiattaforma

logger = logging.getLogger(__name__)


# ── Costanti stati ────────────────────────────────────────────────────────────

STATUS_AVAILABLE   = "available"
STATUS_SOLD_OUT    = "sold_out"
STATUS_UNAVAILABLE = "unavailable"
STATUS_UNKNOWN     = "unknown"

ALL_STATUSES = {STATUS_AVAILABLE, STATUS_SOLD_OUT, STATUS_UNAVAILABLE, STATUS_UNKNOWN}

# Stati da cui una transizione a "available" è considerata un alert
ALERT_TRIGGER_PREVIOUS_STATUSES = {STATUS_SOLD_OUT, STATUS_UNAVAILABLE, STATUS_UNKNOWN, None}

# ── Costanti sale_status ───────────────────────────────────────────────────────

# Valori sale_status Vivaticket che indicano disponibilità piena e confermata
SALE_STATUS_AVAILABLE_VALUES = {"available", "on_sale", "onsale", "in_vendita"}

# Valori sale_status Vivaticket che indicano disponibilità speciale o condizionata.
# Non sono sold_out, ma non sono nemmeno un "available" standard.
# Esempi noti: "available_or_special", "presale", "special", "member_only", "vip"
# Trattati come available se almeno un segnale secondario lo conferma.
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
    "waitlist",        # lista d'attesa = potenzialmente disponibile
}

# Valori sale_status Vivaticket che indicano sold out o non disponibilità esplicita
SALE_STATUS_SOLDOUT_VALUES = {
    "sold_out", "soldout", "esaurito",
    "unavailable", "not_available",
    "cancelled", "canceled", "annullato",
    "postponed",   # rimandato = non vendibile ora
}

# ── Costanti performance_status numerico ──────────────────────────────────────

# performance_status Vivaticket noti — aggiornare man mano che se ne scoprono nuovi.
#
# IMPORTANTE: usare SOLO come hard-block i codici che indicano con certezza
# che l'evento non esiste più o è stato cancellato.
# Codici sconosciuti NON devono bloccare — potrebbero essere nuove varianti attive.
#
# 100 = attivo/normale (confermato)
# 102 = variante attiva, es. prevendita speciale o evento con condizioni (confermato)
# 110 = rescheduled (rinviato, ma l'evento esiste) — trattato come active
# 200 = chiuso/terminato (vendita finita, evento passato)
# 201 = annullato con rimborso in corso
# 999 = cancellato definitivo (ipotetico)
PERF_STATUS_ACTIVE  = {100, 102, 110}   # vendita attiva in tutte le sue forme
PERF_STATUS_CLOSED  = {200, 201}        # evento passato/chiuso (non cancellato)
PERF_STATUS_HARD_UNAVAILABLE = set()    # hard block: nessuno per ora — espandere solo con certezza documentata

# ── Costanti Performance model ────────────────────────────────────────────────

# Valori Performance.status che indicano disponibilità
PERFORMANCE_STATUS_AVAILABLE = {"onsale", "on_sale", "available"}

# Valori Performance.disponibilita_agg che indicano disponibilità
DISPONIBILITA_AVAILABLE = {"disponibile", "available", "in_vendita"}

# Valori Performance.disponibilita_agg che indicano sold out
DISPONIBILITA_SOLDOUT = {"esaurito", "sold_out", "soldout", "non_disponibile", "non disponibile"}


# ── Logica rilevamento stato ──────────────────────────────────────────────────

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
      - I segnali "negativi certi" (sold_out esplicito, cancellazione confermata,
        is_sell_active=False) bloccano presto e forte.
      - I codici performance_status sconosciuti NON bloccano: potrebbero essere
        nuove varianti attive non ancora mappate. Si degradano a unknown, non
        a unavailable, salvo siano nella lista PERF_STATUS_HARD_UNAVAILABLE.
      - I sale_status "speciali" (available_or_special, presale...) vengono trattati
        come available se almeno un segnale secondario lo conferma.
      - In caso di incongruenza tra fonti, si preferisce unknown a unavailable
        per non generare falsi negativi.

    Logica a livelli:

      Livello 0 — hard block performance_status (solo codici certi di cancellazione)
        performance_status in PERF_STATUS_HARD_UNAVAILABLE → unavailable

      Livello 1 — sale_status sold_out esplicito
        sale_status in SALE_STATUS_SOLDOUT_VALUES → sold_out

      Livello 2 — is_sell_active == False (vendita disattivata)
        False + disponibilita_agg=disponibile → unknown (dati incongruenti)
        False → unavailable

      Livello 3 — sale_status standard available + is_sell_active=True
        → available (salvo disponibilita_agg=soldout → sold_out)

      Livello 4 — sale_status speciale + is_sell_active=True
        Con conferma secondaria (disponibilita_agg=disponibile o Performance.status=ONSALE)
        → available
        Senza conferma secondaria → unknown

      Livello 5 — performance_status sconosciuto + is_sell_active=True
        Con conferma da disponibilita_agg o Performance.status → unknown (non available)
        L'incertezza sul codice non ci permette di confermare, ma non blocchiamo.

      Livello 6 — fallback su Performance.disponibilita_agg
        disponibile → available | esaurito → sold_out

      Livello 7 — fallback su Performance.status
        ONSALE → available

      Livello 8 — nessun segnale affidabile → unknown
    """

    sale_status_raw    = snapshot.get("sale_status", "")
    sale_status        = (sale_status_raw or "").lower().strip()
    is_sell_active     = snapshot.get("is_sell_active")
    performance_status = snapshot.get("performance_status")

    # Normalizza performance_status a intero (None se non presente o non numerico)
    ps: int | None = None
    if performance_status is not None:
        try:
            ps = int(performance_status)
        except (TypeError, ValueError):
            ps = None

    # Campi Performance (possono essere None se non collegato)
    perf_status_raw        = None
    perf_disponibilita_raw = None
    if performance is not None:
        perf_status_raw        = getattr(performance, "status", None)
        perf_disponibilita_raw = getattr(performance, "disponibilita_agg", None)

    perf_status        = (perf_status_raw        or "").lower().strip()
    perf_disponibilita = (perf_disponibilita_raw or "").lower().strip()

    # Segnali secondari positivi: almeno uno deve essere True per confermare
    # uno stato "dubbio" (sale_status speciale, performance_status sconosciuto)
    has_positive_secondary = (
        perf_disponibilita in DISPONIBILITA_AVAILABLE
        or perf_status in PERFORMANCE_STATUS_AVAILABLE
    )

    # ── Livello 0: hard block performance_status certi ────────────────────
    # Solo codici documentati come cancellazione definitiva.
    # Per ora PERF_STATUS_HARD_UNAVAILABLE è vuoto — espandere con certezza.
    if ps is not None and ps in PERF_STATUS_HARD_UNAVAILABLE:
        return (
            STATUS_UNAVAILABLE,
            f"performance_status={ps} in PERF_STATUS_HARD_UNAVAILABLE (cancellato)"
        )

    # ── Livello 1: sold_out esplicito da sale_status ──────────────────────
    if sale_status in SALE_STATUS_SOLDOUT_VALUES:
        return (
            STATUS_SOLD_OUT,
            f"sale_status='{sale_status_raw}'"
        )

    # ── Livello 2: vendita disattivata ────────────────────────────────────
    if is_sell_active is False:
        if perf_disponibilita in DISPONIBILITA_AVAILABLE:
            # Incongruenza: is_sell_active=False ma Performance dice disponibile.
            # Potrebbe essere un ritardo di sincronizzazione. Non bloccare.
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
    # Es: "available_or_special", "presale", "vip", ecc.
    # Trattato come available solo se almeno un segnale secondario lo conferma.
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
                f"disponibilita_agg='{perf_disponibilita_raw}' Performance.status='{perf_status_raw}'"
                + (f" performance_status={ps}" if ps is not None else "")
            )
        # sale_status speciale ma nessuna conferma secondaria → prudenza
        return (
            STATUS_UNKNOWN,
            f"sale_status='{sale_status_raw}' (speciale) senza conferma secondaria"
            + (f" performance_status={ps}" if ps is not None else "")
        )

    # ── Livello 5: performance_status sconosciuto + is_sell_active=True ───
    # Codice numerico non in nessuna lista nota.
    # Non blocchiamo (potrebbe essere una nuova variante attiva), ma non confermiamo.
    # Lasciamo passare ai livelli successivi (fallback su Performance).
    if ps is not None and ps not in PERF_STATUS_ACTIVE and ps not in PERF_STATUS_CLOSED:
        # Nota: se is_sell_active è True e c'è conferma secondaria,
        # i livelli successivi possono ancora restituire available.
        # Se tutto fallisce, torneremo unknown — mai unavailable per ps sconosciuto.
        pass  # continua ai livelli 6-8

    # ── Livello 6: fallback su Performance.disponibilita_agg ─────────────
    if perf_disponibilita in DISPONIBILITA_AVAILABLE:
        return (
            STATUS_AVAILABLE,
            f"disponibilita_agg='{perf_disponibilita_raw}' "
            f"(sale_status='{sale_status_raw}'"
            + (f" performance_status={ps}" if ps is not None else "")
            + ")"
        )

    if perf_disponibilita in DISPONIBILITA_SOLDOUT:
        return (
            STATUS_SOLD_OUT,
            f"disponibilita_agg='{perf_disponibilita_raw}'"
        )

    # ── Livello 7: fallback su Performance.status ─────────────────────────
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
    True se la transizione di stato è un evento rilevante per un alert PRO.
    Condizione: si passa da uno stato non-disponibile a "available".

    Prevenzione falsi alert:
      - previous_status == None: primo scan assoluto → NON è un alert
        (non sappiamo lo stato reale precedente, potrebbe essere sempre stato available)
      - previous_status == "available": nessuna transizione
      - Transizioni available → available: ignorate
    """
    if previous_status is None:
        # Primo scan: non generiamo alert per non inondare di notifiche
        # su eventi che erano già disponibili prima che lo scanner partisse
        return False

    return (
        previous_status in ALERT_TRIGGER_PREVIOUS_STATUSES
        and new_status == STATUS_AVAILABLE
    )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_title(pp) -> str:
    """Recupera il titolo dell'evento dal grafo PP → Performance → Evento."""
    try:
        # Prima scelta: titolo diretto in snapshot (sempre presente nell'importer)
        title = (pp.snapshot_raw or {}).get("title", "")
        if title:
            return title

        perf = pp.performance
        if perf is None:
            return f"pp_id={pp.id}"

        if hasattr(perf, "evento") and perf.evento:
            return getattr(perf.evento, "titolo", None) or f"evento_id={perf.evento_id}"

        if hasattr(perf, "titolo") and perf.titolo:
            return perf.titolo

        return f"performance_id={perf.id}"
    except Exception:
        return f"pp_id={pp.id}"


def _get_event_info(pp) -> dict:
    """Raccoglie info evento per log/alert da snapshot_raw."""
    snap = pp.snapshot_raw or {}
    return {
        "title":   snap.get("title", "-"),
        "city":    snap.get("city", "-"),
        "venue":   snap.get("venue", "-"),
        "date":    snap.get("raw_date") or snap.get("starts_at_raw", "-"),
        "pcode":   snap.get("pcode", "-"),
        "tcode":   snap.get("tcode", "-"),
        "shop_url": snap.get("shop_url", "-"),
        "source_url": snap.get("source_url", "-"),
    }


# ── Notifica PRO ──────────────────────────────────────────────────────────────

def trigger_alert(pp, new_status: str, previous_status: str, reason: str, dry_run: bool):
    """
    Placeholder notifica PRO per transizione → available.

    Da implementare con la stessa logica usata per TicketOne/Ticketmaster:
      - Email PRO agli utenti che hanno l'alert attivo su questo evento
      - Push notification
      - Webhook

    Args:
        pp:              PerformancePiattaforma instance
        new_status:      nuovo stato rilevato ("available")
        previous_status: stato precedente
        reason:          stringa diagnostica dal detector
        dry_run:         se True, logga solo senza inviare
    """
    info = _get_event_info(pp)

    logger.info(
        "[ALERT] pp_id=%s | %s → %s | %s @ %s %s | reason: %s",
        pp.id, previous_status, new_status,
        info["title"], info["city"], info["date"], reason,
    )

    if dry_run:
        return

    # TODO: implementare invio email/push/webhook PRO
    # Esempio:
    #   from api.tasks import send_availability_alert
    #   send_availability_alert.delay(
    #       pp_id=pp.id,
    #       platform="vivaticket",
    #       new_status=new_status,
    #       previous_status=previous_status,
    #       event_info=info,
    #   )
    pass


# ── Management command ────────────────────────────────────────────────────────

class Command(BaseCommand):
    help = (
        "Scanner PRO Vivaticket — determina disponibilità da snapshot_raw + Performance. "
        "Non fa chiamate HTTP (Incapsula). Rileva transizioni per alert PRO."
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
            help="Non salva nulla sul DB e non invia alert",
        )
        parser.add_argument(
            "--verbose", action="store_true",
            help="Log esteso con tutti i campi rilevanti",
        )
        parser.add_argument(
            "--alert-on-transition", action="store_true", default=False,
            help="Attiva notifica PRO quando lo stato passa a 'available'",
        )
        parser.add_argument(
            "--reset-status", action="store_true", default=False,
            help="Resetta last_vivaticket_status a None (utile per re-test alert)",
        )

    def handle(self, *args, **options):
        limit               = options["limit"]
        only_id             = options["only_id"]
        dry_run             = options["dry_run"]
        verbose             = options["verbose"]
        alert_on_transition = options["alert_on_transition"]
        reset_status        = options["reset_status"]

        self.stdout.write(self.style.SUCCESS("[START] scan_vivaticket_pro"))
        self.stdout.write(f"[TIME]   {timezone.now().isoformat()}")
        self.stdout.write(
            f"[CONFIG] limit={limit} dry_run={dry_run} only_id={only_id} "
            f"alert_on_transition={alert_on_transition} reset_status={reset_status}"
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
        processed         = 0
        skipped           = 0
        count_available   = 0
        count_sold_out    = 0
        count_unavailable = 0
        count_unknown     = 0
        count_unchanged   = 0
        count_transitioned = 0
        alerts_triggered  = 0
        errors            = 0

        # ── Loop principale ───────────────────────────────────────────────
        for pp in qs:
            if processed >= limit:
                break

            snapshot  = pp.snapshot_raw or {}
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

                # ── Contatori ─────────────────────────────────────────────
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
                            f"         Performance.status={getattr(performance, 'status', None)!r} "
                            f"disponibilita_agg={getattr(performance, 'disponibilita_agg', None)!r} "
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
                        trigger_alert(pp, new_status, previous_status, reason, dry_run)
                        alerts_triggered += 1
                    except Exception as exc:
                        self.stdout.write(self.style.ERROR(
                            f"[ALERT ERROR] pp_id={pp.id} error={exc}"
                        ))

                # ── Salvataggio DB ────────────────────────────────────────
                if not dry_run:
                    new_snapshot = dict(snapshot)
                    new_snapshot["last_scan_vivaticket_pro"]   = timezone.now().isoformat()
                    new_snapshot["last_vivaticket_status"]     = new_status
                    new_snapshot["last_vivaticket_reason"]     = reason
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
            f"status_changed={count_transitioned} unchanged={count_unchanged} "
            f"alerts_triggered={alerts_triggered}"
        )