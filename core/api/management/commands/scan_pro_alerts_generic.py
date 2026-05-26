import time
from datetime import timedelta
from typing import Tuple

from django.conf import settings
from django.core.mail import send_mail
from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

# Piattaforme non ancora supportate dallo scanner generico.
# Vengono skippate prima di chiamare check_platform_availability,
# evitando log inutili e falsi contatori.
_NOT_READY_PLATFORMS = {"fansale", "vivaticket"}


class Command(BaseCommand):
    help = "Scanner generico PRO: legge i monitoraggi PRO e controlla le piattaforme collegate."

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit",
            type=int,
            default=50,
            help="Numero massimo di monitoraggi da processare."
        )

        parser.add_argument(
            "--sleep",
            type=float,
            default=0.0,
            help="Pausa tra un controllo e l'altro."
        )

        parser.add_argument(
            "--verbose",
            action="store_true",
            help="Mostra più dettagli durante l'esecuzione."
        )

        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Modalità test: non invia email e non salva notifiche."
        )

        parser.add_argument(
            "--force-available-platform",
            type=str,
            default=None,
            help="Solo test: forza una piattaforma a risultare available. Esempio: ticketmaster"
        )

        parser.add_argument(
            "--email-retries",
            type=int,
            default=3,
            help="Numero massimo di tentativi invio email."
        )

        parser.add_argument(
            "--email-wait",
            type=float,
            default=1.5,
            help="Attesa base tra i tentativi email."
        )

    def handle(self, *args, **options):
        limit = options["limit"]
        sleep_seconds = options["sleep"]
        verbose = options["verbose"]
        dry_run = options["dry_run"]
        force_available_platform = options.get("force_available_platform")
        email_retries = max(1, int(options.get("email_retries") or 3))
        email_wait = max(0.5, float(options.get("email_wait") or 1.5))

        if force_available_platform:
            force_available_platform = force_available_platform.strip().lower()
        now = timezone.now()

        self.stdout.write(self.style.SUCCESS("[START] scan_pro_alerts_generic"))
        self.stdout.write(f"[TIME] {now.isoformat()}")
        self.stdout.write(f"[CONFIG] limit={limit} sleep={sleep_seconds} dry_run={dry_run}")

        from api.models import (
            Monitoraggio,
            EventoPiattaforma,
            PerformancePiattaforma,
            Notifica,
        )

        qs = (
            Monitoraggio.objects
            .select_related(
                "abbonamento",
                "abbonamento__utente",
                "abbonamento__plan",
                "performance",
                "performance__evento",
                "performance__luogo",
                "evento",
            )
            .filter(abbonamento__attivo=True)
            .filter(abbonamento__plan__plan_type="PRO")
            .filter(abbonamento__prezzo__gt=0)
            .filter(
                Q(abbonamento__data_fine__isnull=True) |
                Q(abbonamento__data_fine__gte=now)
            )
            .order_by("id")[:limit]
        )

        total = qs.count()

        self.stdout.write(f"[PRO] monitoraggi PRO attivi trovati: {total}")

        # --- contatori principali ---
        processed = 0
        skipped_no_target = 0
        skipped_no_platform = 0
        links_found = 0

        # --- contatori per piattaforma ---
        ticketmaster_count = 0
        ticketone_count = 0
        fansale_count = 0
        other_count = 0

        # --- contatori per esito ---
        notified = 0            # email inviata con successo
        deduped = 0             # già notificato oggi, skip
        unknown_count = 0       # availability=unknown (inclusi 401)
        unavailable_count = 0   # availability=unavailable
        skipped_not_ready = 0   # piattaforma non ancora supportata

        for monitoraggio in qs:
            processed += 1

            abbonamento = monitoraggio.abbonamento
            utente = abbonamento.utente

            performance = monitoraggio.performance
            evento = monitoraggio.evento

            if performance and not evento:
                evento = performance.evento

            if not performance and not evento:
                skipped_no_target += 1
                self.stdout.write(
                    self.style.WARNING(
                        f"[SKIP] monitoraggio={monitoraggio.id}: nessun evento/performance"
                    )
                )
                continue

            self.stdout.write("")
            self.stdout.write(
                f"[MONITORAGGIO] id={monitoraggio.id} "
                f"utente_id={utente.id} "
                f"email={utente.email} "
                f"abbonamento_id={abbonamento.id} "
                f"performance_id={performance.id if performance else None} "
                f"evento_id={evento.id if evento else None}"
            )

            if evento:
                self.stdout.write(f"[EVENTO] {evento.nome_evento}")

            if performance:
                luogo_nome = performance.luogo.nome if performance.luogo else "-"
                self.stdout.write(
                    f"[PERFORMANCE] id={performance.id} "
                    f"data={performance.starts_at_utc} "
                    f"luogo={luogo_nome}"
                )

            platform_links = []
            seen_links = set()

            def add_platform_link(source, link):
                """
                Aggiunge un mapping piattaforma evitando duplicati.

                Lo scanner generico deve controllare tutte le piattaforme collegate:
                - mapping della performance
                - mapping dell'evento
                - mapping di eventuali performance equivalenti
                """

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
                    "mapping": link,
                })

            """
            STEP IMPORTANTE:
            cerchiamo performance equivalenti.

            Esempio:
            - l'utente monitora Annalisa da fanSALE
            - nel DB esiste Annalisa stessa data/città anche da Ticketmaster
            - lo scanner deve controllare anche Ticketmaster
            """

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
                    .filter(performance=eq_perf)
                    .filter(piattaforma__attivo=True)
                )

                for link in perf_links:
                    add_platform_link(f"performance:{eq_perf.id}", link)

            if equivalent_event_ids:
                event_links = (
                    EventoPiattaforma.objects
                    .select_related("piattaforma")
                    .filter(evento_id__in=equivalent_event_ids)
                    .filter(piattaforma__attivo=True)
                )

                for link in event_links:
                    add_platform_link(f"evento:{link.evento_id}", link)

            if not platform_links:
                skipped_no_platform += 1
                self.stdout.write(
                    self.style.WARNING(
                        f"[SKIP] monitoraggio={monitoraggio.id}: nessuna piattaforma collegata"
                    )
                )
                continue

            for link in platform_links:
                links_found += 1

                platform_name = link["platform_name"]
                url = link["url"]
                source = link["source"]

                if platform_name == "ticketmaster":
                    ticketmaster_count += 1
                elif platform_name == "ticketone":
                    ticketone_count += 1
                elif platform_name == "fansale":
                    fansale_count += 1
                else:
                    other_count += 1

                if not url:
                    self.stdout.write(
                        self.style.WARNING(
                            f"[SKIP URL] monitoraggio={monitoraggio.id} "
                            f"platform={platform_name}: url vuoto"
                        )
                    )
                    continue

                self.stdout.write(
                    f"[LINK] source={source} "
                    f"platform={platform_name} "
                    f"url={url}"
                )

                # PATCH 2: skip anticipato per piattaforme non ancora supportate.
                # Evitiamo di chiamare check_platform_availability e di generare
                # log "availability=unknown reason=fansale_checker_not_ready" per
                # ogni monitoraggio, che non porta nessuna informazione utile.
                if platform_name in _NOT_READY_PLATFORMS:
                    skipped_not_ready += 1
                    self.stdout.write(
                        self.style.WARNING(
                            f"[SKIP NOT READY] platform={platform_name} "
                            f"monitoraggio={monitoraggio.id}: checker non ancora disponibile"
                        )
                    )
                    if sleep_seconds > 0:
                        time.sleep(sleep_seconds)
                    continue

                result = check_platform_availability(
                    platform_name=platform_name,
                    url=url,
                    verbose=verbose,
                )
                if force_available_platform and platform_name == force_available_platform:
                    result = {
                        "ok": True,
                        "availability": "available",
                        "reason": "FORCE_AVAILABLE",
                        "status_code": result.get("status_code"),
                        "final_url": result.get("final_url", url),
                        "min_price": result.get("min_price"),
                        "currency": result.get("currency"),
                        "raw_price_text": result.get("raw_price_text"),
                        "raw": result,
                    }

                    self.stdout.write(
                        self.style.WARNING(
                            f"[FORCE AVAILABLE] platform={platform_name} monitoraggio={monitoraggio.id}"
                        )
                    )

                status_code = result.get("status_code")

                self.stdout.write(
                    f"[RESULT] platform={platform_name} "
                    f"availability={result['availability']} "
                    f"reason={result['reason']} "
                    f"status_code={status_code}"
                )

                # PATCH 3: HTTP 401 — log esplicito e skip immediato.
                # Ticketmaster .com risponde 401 su alcuni URL internazionali.
                # Non è un segnale di disponibilità né di indisponibilità:
                # trattiamo come unknown e non notifichiamo.
                if status_code == 401:
                    unknown_count += 1
                    self.stdout.write(
                        self.style.WARNING(
                            f"[SKIP 401] platform={platform_name} "
                            f"monitoraggio={monitoraggio.id} url={url}"
                        )
                    )
                    if sleep_seconds > 0:
                        time.sleep(sleep_seconds)
                    continue

                if result["availability"] == "available":
                    dedupe_key = build_generic_dedupe_key(
                        monitoraggio=monitoraggio,
                        user=utente,
                        platform_name=platform_name,
                        result=result,
                    )

                    already_sent = Notifica.objects.filter(
                        dedupe_key=dedupe_key,
                        status="SENT",
                    ).exists()

                    if already_sent:
                        deduped += 1  # PATCH 6: contatore dedup
                        self.stdout.write(
                            self.style.WARNING(
                                f"[DEDUP] monitoraggio={monitoraggio.id} "
                                f"user={utente.id} "
                                f"platform={platform_name} "
                                f"dedupe={dedupe_key}"
                            )
                        )

                        if sleep_seconds > 0:
                            time.sleep(sleep_seconds)

                        continue

                    subject, message = build_generic_email_message(
                        user=utente,
                        monitoraggio=monitoraggio,
                        performance=performance,
                        evento=evento,
                        platform_name=platform_name,
                        url=url,
                        result=result,
                    )
                    if dry_run:
                        self.stdout.write(
                            self.style.WARNING(
                                f"[DRY-RUN EMAIL] to={utente.email} "
                                f"subject={subject} "
                                f"dedupe={dedupe_key}"
                            )
                        )
                    else:
                        if not getattr(utente, "notify_email", True):
                            self.stdout.write(
                                self.style.WARNING(
                                    f"[SKIP EMAIL PREF] user={utente.id} email={utente.email}"
                                )
                            )
                            if sleep_seconds > 0:
                                time.sleep(sleep_seconds)
                            continue

                        to_email = getattr(utente, "email", None)

                        if not to_email:
                            self.stdout.write(
                                self.style.WARNING(
                                    f"[SKIP NO EMAIL] user={utente.id}"
                                )
                            )
                            if sleep_seconds > 0:
                                time.sleep(sleep_seconds)
                            continue

                        ok, err = send_email_with_retry(
                            subject=subject,
                            message=message,
                            to_email=to_email,
                            max_retries=email_retries,
                            base_wait=email_wait,
                        )

                        with transaction.atomic():
                            if ok:
                                Notifica.objects.create(
                                    monitoraggio=monitoraggio,
                                    channel="email",
                                    dedupe_key=dedupe_key,
                                    status="SENT",
                                    message=message,
                                )

                                notified += 1  # PATCH 6: contatore notifiche inviate

                                self.stdout.write(
                                    self.style.SUCCESS(
                                        f"[EMAIL SENT] monitoraggio={monitoraggio.id} "
                                        f"user={utente.id} to={to_email} dedupe={dedupe_key}"
                                    )
                                )
                            else:
                                Notifica.objects.create(
                                    monitoraggio=monitoraggio,
                                    channel="email",
                                    dedupe_key=dedupe_key,
                                    status="FAILED",
                                    message=f"{message}\n\nERRORE INVIO EMAIL:\n{err}",
                                )

                                self.stdout.write(
                                    self.style.ERROR(
                                        f"[EMAIL FAIL] monitoraggio={monitoraggio.id} "
                                        f"user={utente.id} to={to_email} err={err}"
                                    )
                                )

                elif result["availability"] == "unavailable":
                    unavailable_count += 1  # PATCH 6

                else:
                    # availability=unknown: nessuna azione, solo contatore
                    unknown_count += 1  # PATCH 6

                if sleep_seconds > 0:
                    time.sleep(sleep_seconds)

        # PATCH 6: riepilogo finale con contatori chiari
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("[DONE]"))
        self.stdout.write(f"processed           ={processed}")
        self.stdout.write(f"links_found         ={links_found}")
        self.stdout.write(f"  ticketmaster_links ={ticketmaster_count}")
        self.stdout.write(f"  ticketone_links    ={ticketone_count}")
        self.stdout.write(f"  fansale_links      ={fansale_count}")
        self.stdout.write(f"  other_links        ={other_count}")
        self.stdout.write("")
        self.stdout.write(f"notified            ={notified}")
        self.stdout.write(f"deduped             ={deduped}")
        self.stdout.write(f"unknown             ={unknown_count}")
        self.stdout.write(f"unavailable         ={unavailable_count}")
        self.stdout.write(f"skipped_not_ready   ={skipped_not_ready}")
        self.stdout.write(f"skipped_no_target   ={skipped_no_target}")
        self.stdout.write(f"skipped_no_platform ={skipped_no_platform}")


def normalize_platform_name(name):
    """
    Normalizza il nome della piattaforma.

    Esempi:
    - TicketMaster diventa ticketmaster
    - TicketOne diventa ticketone
    - fanSALE diventa fansale
    """

    if not name:
        return ""

    return str(name).strip().lower()


def get_link_url(link):
    """
    Recupera l'URL dal mapping.

    Sia EventoPiattaforma sia PerformancePiattaforma
    hanno il campo url.
    """

    url = getattr(link, "url", "")

    if not url:
        return ""

    return str(url).strip()


def find_equivalent_performances(performance):
    """
    Cerca performance equivalenti alla performance monitorata.

    Serve per il caso:
    - utente monitora un evento nato da fanSALE
    - lo stesso evento/data/città esiste anche su Ticketmaster o TicketOne
    - vogliamo controllare anche quelle piattaforme

    Criteri prudenti:
    1. stesso nome evento normalizzato
    2. data entro una finestra di 12 ore
    3. stessa città se la città è disponibile

    Ritorna una lista di Performance.
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


def check_platform_availability(platform_name, url, verbose=False):
    """
    Dispatcher generico.

    In base alla piattaforma, chiama il controllo corretto.
    Tutti i controlli restituiscono un dizionario standard.

    Nota: fansale e vivaticket sono in _NOT_READY_PLATFORMS e vengono
    skippate prima di arrivare qui. Questi branch restano come fallback
    nel caso in cui _NOT_READY_PLATFORMS venga modificata.
    """

    if platform_name == "ticketmaster":
        return check_ticketmaster(url=url, verbose=verbose)

    if platform_name == "ticketone":
        return check_ticketone(url=url, verbose=verbose)

    if platform_name == "fansale":
        return {
            "ok": True,
            "availability": "unknown",
            "reason": "fansale_checker_not_ready",
            "status_code": None,
            "final_url": url,
        }

    if platform_name == "vivaticket":
        return {
            "ok": True,
            "availability": "unknown",
            "reason": "vivaticket_checker_not_ready",
            "status_code": None,
            "final_url": url,
        }

    return {
        "ok": True,
        "availability": "unknown",
        "reason": f"unsupported_platform_{platform_name}",
        "status_code": None,
        "final_url": url,
    }


def check_ticketmaster(url, verbose=False):
    """
    Controllo reale Ticketmaster.

    Usa la funzione già presente nel progetto:
    api.scrapers.ticketmaster_availability.check_ticketmaster_page_availability

    Nota:
    la funzione Ticketmaster deve essere già stata resa prudente:
    - negative keyword => unavailable
    - strong positive keyword => available
    - weak positive keyword => unknown
    """

    from api.scrapers.ticketmaster_availability import check_ticketmaster_page_availability

    res = check_ticketmaster_page_availability(
        url=url,
        timeout=20,
        session=None,
        max_retries=2,
    )

    return {
        "ok": res.get("ok", False),
        "availability": res.get("availability", "unknown"),
        "reason": res.get("reason", "no_reason"),
        "status_code": res.get("status_code"),
        "final_url": res.get("final_url", url),
        "raw": res,
    }


def check_ticketone(url, verbose=False):
    """
    Controllo reale TicketOne leggero per scanner generico.

    Qui NON usiamo browser fallback, perché lo scanner generico dovrà girare spesso.
    Se TicketOne risponde con errore o pagina non leggibile, ritorniamo unknown.
    """

    from api.scrapers.ticketone.ticketone_prices import get_ticketone_price_data

    try:
        price_data = get_ticketone_price_data(
            url,
            verbose=False,
            use_browser_fallback=False,
            browser_headless=True,
        )

        is_available = ticketone_result_is_available(price_data)

        if is_available:
            reason = build_ticketone_reason(price_data)

            return {
                "ok": True,
                "availability": "available",
                "reason": reason,
                "status_code": price_data.get("status_code"),
                "final_url": price_data.get("final_url", url),
                "min_price": price_data.get("min_price"),
                "currency": price_data.get("currency"),
                "raw_price_text": price_data.get("raw_price_text"),
                "detail_status": price_data.get("detail_status"),
                "source_used": price_data.get("source_used"),
                "raw": price_data,
            }

        return {
            "ok": True,
            "availability": "unknown",
            "reason": build_ticketone_reason(price_data),
            "status_code": price_data.get("status_code"),
            "final_url": price_data.get("final_url", url),
            "min_price": price_data.get("min_price"),
            "currency": price_data.get("currency"),
            "raw_price_text": price_data.get("raw_price_text"),
            "detail_status": price_data.get("detail_status"),
            "source_used": price_data.get("source_used"),
            "raw": price_data,
        }

    except Exception as exc:
        return {
            "ok": False,
            "availability": "unknown",
            "reason": f"ticketone_exception: {exc}",
            "status_code": None,
            "final_url": url,
        }


def ticketone_result_is_available(result):
    """
    Segnale forte richiesto per dichiarare available un risultato TicketOne.

    Una pagina può essere letta correttamente (detail_status=ok) ma non avere
    nessun prezzo esposto: in quel caso non è un segnale di disponibilità reale
    e restituiamo unknown per evitare falsi positivi.

    Condizioni sufficienti:
    - min_price presente  → prezzo numerico estratto
    - raw_price_text presente → testo prezzo trovato nel DOM

    detail_status=ok da solo NON è sufficiente: viene loggato da
    build_ticketone_reason ma non usato per inviare alert.
    """

    if result.get("min_price") is not None:
        return True

    if result.get("raw_price_text"):
        return True

    return False


def send_email_with_retry(
    *,
    subject: str,
    message: str,
    to_email: str,
    max_retries: int,
    base_wait: float,
) -> Tuple[bool, str]:
    """
    Invia una email con piccoli retry.

    Ritorna:
    - True, "" se inviata correttamente
    - False, errore se fallisce
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

            if attempt < max_retries:
                time.sleep(base_wait * attempt)

    return False, last_err


def build_ticketone_reason(result):
    """
    Crea una reason leggibile per il log.
    """

    if result.get("min_price") is not None:
        return "ticketone_min_price_found"

    if result.get("raw_price_text"):
        return "ticketone_raw_price_text_found"

    if result.get("detail_status") == "ok":
        return "ticketone_detail_status_ok"

    detail_status = result.get("detail_status") or "no_detail_status"
    source_used = result.get("source_used") or "no_source"

    return f"ticketone_no_strong_signal:{detail_status}:{source_used}"


def build_generic_dedupe_key(*, monitoraggio, user, platform_name, result):
    """
    Chiave giornaliera per deduplicare notifiche.

    La `reason` è esclusa intenzionalmente: lo stesso evento/piattaforma/giorno
    non deve generare più notifiche anche se la reason cambia tra run distinti
    (es. "strong_positive_keyword" vs "ticketone_min_price_found").
    Includere la reason nella chiave causerebbe notifiche duplicate per lo
    stesso utente/evento nello stesso giorno.

    Formato:
    generic:{platform_name}:mon:{monitoraggio.id}:user:{user.id}:{YYYY-MM-DD}

    Esempio:
    generic:ticketmaster:mon:51:user:43:2026-05-05
    """

    day = timezone.now().date().isoformat()

    return (
        f"generic:{platform_name}:"
        f"mon:{monitoraggio.id}:user:{user.id}:{day}"
    )


def build_generic_email_message(
    *,
    user,
    monitoraggio,
    performance,
    evento,
    platform_name,
    url,
    result,
):
    """
    Costruisce subject e body email generica.

    Questa funzione non invia nulla.
    Prepara solo il testo.
    """

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
    reason = result.get("reason") or "available"

    message = f"""Ciao,

abbiamo trovato una possibile disponibilità per il tuo monitoraggio PRO.

Evento: {event_name}
Luogo: {luogo}
Data: {data_evento}
Piattaforma: {platform_label}

"""

    if min_price is not None:
        message += f"Prezzo rilevato: da {min_price} {currency}\n"
    elif raw_price_text:
        message += f"Prezzo rilevato: {raw_price_text}\n"
    else:
        message += "Prezzo: non disponibile o non rilevato\n"

    message += f"""
Stato controllo: {reason}

Link:
{url}

Grazie,
Tixy
"""

    return subject, message