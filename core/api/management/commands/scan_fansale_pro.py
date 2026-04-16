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

from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.firefox.options import Options

from api.models import (
    Abbonamento,
    Biglietto,
    EventoPiattaforma,
    Listing,
    Monitoraggio,
    Notifica,
    Performance,
    PerformancePiattaforma,
)


# =========================================================
# Helpers
# =========================================================

def _abbonamento_is_active(ab: Abbonamento) -> bool:
    if not getattr(ab, "attivo", False):
        return False

    data_fine = getattr(ab, "data_fine", None)
    if data_fine and data_fine < timezone.now():
        return False

    return True


def _dedupe_key(perf_id: int, user_id: int, platform: str, reason: str) -> str:
    day = timezone.now().date().isoformat()
    return f"{platform}:{reason}:perf:{perf_id}:user:{user_id}:{day}"


def _has_internal_tickets(perf: Performance) -> bool:
    if Biglietto.objects.filter(performance=perf, is_valid=True).exists():
        return True

    if Listing.objects.filter(performance=perf, status="ACTIVE").exists():
        return True

    return False


def _get_fansale_mapping_for_performance(
    perf: Performance,
) -> Tuple[Optional[str], Optional[str], Optional[int]]:
    """
    Ritorna: (url, mapping_type, mapping_pk)

    mapping_type:
    - "performance" se trovato in PerformancePiattaforma
    - "evento" se trovato in EventoPiattaforma
    - None se assente

    Per fanSALE proviamo prima il mapping a livello performance.
    Se non esiste, facciamo fallback al mapping a livello evento,
    perché attualmente l'import fanSALE popola EventoPiattaforma.
    """
    pp = (
        PerformancePiattaforma.objects
        .filter(
            performance=perf,
            piattaforma__nome__iexact="fansale",
        )
        .exclude(url="")
        .order_by("-aggiornato_il")
        .first()
    )
    if pp and pp.url:
        return pp.url, "performance", pp.pk

    ep = (
        EventoPiattaforma.objects
        .filter(
            evento=perf.evento,
            piattaforma__nome__iexact="fansale",
        )
        .exclude(url="")
        .order_by("-aggiornato_il")
        .first()
    )
    if ep and ep.url:
        return ep.url, "evento", ep.pk

    return None, None, None


def _touch_last_scan(mapping_type: Optional[str], mapping_pk: Optional[int]) -> None:
    if not mapping_type or not mapping_pk:
        return

    now = timezone.now()

    if mapping_type == "performance":
        PerformancePiattaforma.objects.filter(pk=mapping_pk).update(ultima_scansione=now)
    elif mapping_type == "evento":
        EventoPiattaforma.objects.filter(pk=mapping_pk).update(ultima_scansione=now)


def _sleep_with_jitter_fansale(base: float, *, heavy: bool = False) -> None:
    jitter = random.uniform(0.8, 2.4)
    extra = random.uniform(4.0, 9.0) if heavy else 0.0
    time.sleep(max(0.0, base + jitter + extra))


def _send_email_with_retry(
    *,
    subject: str,
    message: str,
    to_email: str,
    max_retries: int,
    base_wait: float,
) -> Tuple[bool, str]:
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
            wait = base_wait * attempt
            time.sleep(wait)

    return False, last_err


def _stable_checksum(value: str) -> str:
    import hashlib
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _normalize_text(text: str) -> str:
    return " ".join((text or "").split()).strip().lower()


def check_fansale_page_availability_selenium(
    *,
    url: str,
    wait_s: int = 8,
    page_load_timeout: int = 30,
    firefox_binary: str = "/snap/firefox/current/usr/lib/firefox/firefox",
) -> dict:
    """
    Check fanSALE via Selenium + Firefox headless.

    Ritorna sempre un dict compatibile con la logica del comando:
    {
        "ok": bool,
        "status_code": int|None,
        "availability": "available" | "unavailable" | "unknown",
        "reason": "...",
        "final_url": "...",
        "checksum": "...",
        "snapshot": {...}
    }
    """
    driver = None

    try:
        firefox_options = Options()
        firefox_options.binary_location = firefox_binary
        firefox_options.add_argument("--headless")

        driver = webdriver.Firefox(options=firefox_options)
        driver.set_page_load_timeout(page_load_timeout)
        driver.get(url)

        if wait_s > 0:
            time.sleep(wait_s)

        title = driver.title or ""
        final_url = driver.current_url or url
        html = driver.page_source or ""

        html_norm = _normalize_text(html)
        title_norm = _normalize_text(title)

        blocked_signals = [
            "challenge page",
            "access denied",
            "temporarily unavailable",
            "forbidden",
            "too many requests",
            "captcha",
            "cf-challenge",
            "attention required",
            "blocked",
        ]

        negative_signals = [
            "non disponibile",
            "sold out",
            "esaurito",
            "non ci sono biglietti",
            "nessun biglietto",
            "nessuna offerta",
            "attualmente non disponibile",
            "non sono disponibili biglietti",
            "nessun risultato",
        ]

        positive_signals = [
            "i migliori biglietti",
            "fansale garantisce biglietti originali",
            "biglietti",
            "tickets",
            "offerte",
            "acquista",
            "compra",
            "posti disponibili",
        ]

        if "challenge page" in title_norm or "challenge page" in html_norm:
            snapshot = {
                "status": "blocked_soft",
                "reason": "challenge_page",
                "http_status": 200,
                "final_url": final_url,
                "page_title": title,
                "checked_at": timezone.now().isoformat(),
            }
            return {
                "ok": False,
                "status_code": 200,
                "availability": "unknown",
                "reason": "challenge_page",
                "final_url": final_url,
                "checksum": _stable_checksum(f"blocked_soft|challenge_page|{final_url}"),
                "snapshot": snapshot,
            }

        for signal in blocked_signals:
            if signal in html_norm or signal in title_norm:
                snapshot = {
                    "status": "blocked_soft",
                    "reason": f"blocked_signal:{signal}",
                    "http_status": 200,
                    "final_url": final_url,
                    "page_title": title,
                    "checked_at": timezone.now().isoformat(),
                }
                return {
                    "ok": False,
                    "status_code": 200,
                    "availability": "unknown",
                    "reason": f"blocked_signal:{signal}",
                    "final_url": final_url,
                    "checksum": _stable_checksum(f"blocked_soft|{signal}|{final_url}"),
                    "snapshot": snapshot,
                }

        found_negative = [s for s in negative_signals if s in html_norm or s in title_norm]
        found_positive = [s for s in positive_signals if s in html_norm or s in title_norm]

        if found_negative and not found_positive:
            reason = f"negative_signal:{found_negative[0]}"
            snapshot = {
                "status": "unavailable",
                "reason": reason,
                "http_status": 200,
                "final_url": final_url,
                "page_title": title,
                "checked_at": timezone.now().isoformat(),
            }
            return {
                "ok": True,
                "status_code": 200,
                "availability": "unavailable",
                "reason": reason,
                "final_url": final_url,
                "checksum": _stable_checksum(f"unavailable|{reason}|{final_url}"),
                "snapshot": snapshot,
            }

        if found_positive:
            reason = f"positive_signal:{found_positive[0]}"
            snapshot = {
                "status": "available",
                "reason": reason,
                "http_status": 200,
                "final_url": final_url,
                "page_title": title,
                "checked_at": timezone.now().isoformat(),
            }
            return {
                "ok": True,
                "status_code": 200,
                "availability": "available",
                "reason": reason,
                "final_url": final_url,
                "checksum": _stable_checksum(f"available|{reason}|{final_url}"),
                "snapshot": snapshot,
            }

        snapshot = {
            "status": "unknown",
            "reason": "html_ambiguous",
            "http_status": 200,
            "final_url": final_url,
            "page_title": title,
            "checked_at": timezone.now().isoformat(),
        }
        return {
            "ok": True,
            "status_code": 200,
            "availability": "unknown",
            "reason": "html_ambiguous",
            "final_url": final_url,
            "checksum": _stable_checksum(f"unknown|html_ambiguous|{final_url}"),
            "snapshot": snapshot,
        }

    except TimeoutException:
        snapshot = {
            "status": "blocked_soft",
            "reason": "selenium_timeout",
            "http_status": None,
            "final_url": url,
            "checked_at": timezone.now().isoformat(),
        }
        return {
            "ok": False,
            "status_code": None,
            "availability": "unknown",
            "reason": "selenium_timeout",
            "final_url": url,
            "checksum": _stable_checksum(f"blocked_soft|selenium_timeout|{url}"),
            "snapshot": snapshot,
        }

    except WebDriverException as e:
        snapshot = {
            "status": "blocked_soft",
            "reason": f"webdriver_error:{e.__class__.__name__}",
            "http_status": None,
            "final_url": url,
            "checked_at": timezone.now().isoformat(),
        }
        return {
            "ok": False,
            "status_code": None,
            "availability": "unknown",
            "reason": f"webdriver_error:{e.__class__.__name__}",
            "final_url": url,
            "checksum": _stable_checksum(f"blocked_soft|webdriver_error|{url}"),
            "snapshot": snapshot,
        }

    except Exception as e:
        snapshot = {
            "status": "blocked_soft",
            "reason": f"exception:{e.__class__.__name__}",
            "http_status": None,
            "final_url": url,
            "checked_at": timezone.now().isoformat(),
        }
        return {
            "ok": False,
            "status_code": None,
            "availability": "unknown",
            "reason": f"exception:{e.__class__.__name__}",
            "final_url": url,
            "checksum": _stable_checksum(f"blocked_soft|exception|{url}"),
            "snapshot": snapshot,
        }

    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass


def _update_mapping_snapshot(mapping_type: Optional[str], mapping_pk: Optional[int], result: dict) -> None:
    if not mapping_type or not mapping_pk:
        return

    if mapping_type == "performance":
        PerformancePiattaforma.objects.filter(pk=mapping_pk).update(
            snapshot_raw=result.get("snapshot"),
            checksum_dati=result.get("checksum"),
        )
    elif mapping_type == "evento":
        EventoPiattaforma.objects.filter(pk=mapping_pk).update(
            snapshot_raw=result.get("snapshot"),
            checksum_dati=result.get("checksum"),
        )


def _is_recently_soft_blocked(
    mapping_type: Optional[str],
    mapping_pk: Optional[int],
    minutes: int = 180,
) -> bool:
    """
    Ritorna True se il mapping ha già dato recentemente un segnale di blocco soft,
    così evitiamo di martellarlo ad ogni run.
    """
    if not mapping_type or not mapping_pk:
        return False

    if mapping_type == "performance":
        obj = PerformancePiattaforma.objects.filter(pk=mapping_pk).values("snapshot_raw").first()
    elif mapping_type == "evento":
        obj = EventoPiattaforma.objects.filter(pk=mapping_pk).values("snapshot_raw").first()
    else:
        return False

    if not obj:
        return False

    snapshot = obj.get("snapshot_raw") or {}
    reason = snapshot.get("reason")
    checked_at = snapshot.get("checked_at")

    if reason not in {"read_timeout", "challenge_page", "selenium_timeout"} and not str(reason).startswith("blocked_signal:"):
        return False

    if not checked_at:
        return False

    try:
        checked_dt = timezone.datetime.fromisoformat(checked_at)
        if timezone.is_naive(checked_dt):
            checked_dt = timezone.make_aware(checked_dt, timezone.get_current_timezone())
    except Exception:
        return False

    return checked_dt >= timezone.now() - timezone.timedelta(minutes=minutes)


# =========================================================
# Command
# =========================================================

class Command(BaseCommand):
    help = "Scansiona monitoraggi PRO attivi e invia email quando fanSALE mostra disponibilità."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=5, help="Quanti monitoraggi massimo processare.")
        parser.add_argument("--sleep", type=float, default=12.0, help="Pausa base tra controlli fanSALE (anti-blocco).")
        parser.add_argument("--dry-run", action="store_true", help="Non invia email e non salva Notifica.")
        parser.add_argument("--verbose", action="store_true", help="Log più dettagliato.")
        parser.add_argument("--email-retries", type=int, default=3, help="Quanti tentativi per inviare email.")
        parser.add_argument("--email-wait", type=float, default=1.5, help="Attesa base tra tentativi email.")
        parser.add_argument("--max-blocked", type=int, default=3, help="Numero massimo di blocchi/errori gravi prima di fermare il job.")
        parser.add_argument("--selenium-wait", type=int, default=8, help="Secondi di attesa dopo il caricamento pagina Selenium.")
        parser.add_argument("--page-timeout", type=int, default=30, help="Timeout di caricamento pagina Selenium.")
        parser.add_argument(
            "--firefox-binary",
            type=str,
            default="/snap/firefox/current/usr/lib/firefox/firefox",
            help="Percorso del binario Firefox reale.",
        )
        parser.add_argument(
            "--soft-block-minutes",
            type=int,
            default=360,
            help="Minuti di pausa per URL fanSALE che hanno già dato blocco soft.",
        )
        parser.add_argument(
            "--perf-id",
            type=int,
            default=None,
            help="Se valorizzato, processa solo questa performance.",
        )

    def handle(self, *args, **opts):
        limit = int(opts["limit"])
        sleep_s = float(opts["sleep"])
        dry_run = bool(opts["dry_run"])
        verbose = bool(opts["verbose"])
        email_retries = max(1, int(opts["email_retries"]))
        email_wait = max(0.5, float(opts["email_wait"]))
        max_blocked = max(1, int(opts["max_blocked"]))
        selenium_wait = max(0, int(opts["selenium_wait"]))
        page_timeout = max(10, int(opts["page_timeout"]))
        firefox_binary = opts["firefox_binary"]
        soft_block_minutes = max(1, int(opts["soft_block_minutes"]))
        perf_id = opts["perf_id"]

        now = timezone.now()

        qs = (
            Monitoraggio.objects
            .filter(
                abbonamento__attivo=True,
                performance__isnull=False,
            )
            .filter(Q(abbonamento__data_fine__isnull=True) | Q(abbonamento__data_fine__gte=now))
            .select_related(
                "abbonamento",
                "abbonamento__utente",
                "abbonamento__plan",
                "performance",
                "performance__evento",
                "performance__luogo",
            )
            .order_by("id")
        )
        if perf_id:
            qs = qs.filter(performance_id=perf_id)
        if verbose:
            self.stdout.write(f"[DEBUG] now={now.isoformat()} qs_count={qs.count()}")

        monitoraggi = []
        for m in qs[: limit * 8]:
            try:
                ab = m.abbonamento
                plan = getattr(ab, "plan", None)

                if not _abbonamento_is_active(ab):
                    continue

                if not plan or getattr(plan, "plan_type", "").upper() != "PRO":
                    continue

                if m.performance is None:
                    continue

                monitoraggi.append(m)
            except Exception:
                continue

            if len(monitoraggi) >= limit:
                break

        self.stdout.write(f"[SCAN] monitoraggi fanSALE PRO attivi: {len(monitoraggi)} (limit={limit})")

        counters = {
            "processed": 0,
            "notified": 0,
            "skip_no_perf": 0,
            "skip_internal": 0,
            "skip_no_mapping": 0,
            "skip_not_avail": 0,
            "skip_dedup": 0,
            "skip_unknown": 0,
            "fansale_error": 0,
            "email_fail": 0,
            "no_email_pref": 0,
            "skip_soft_blocked": 0,
        }

        blocked_events = 0

        for m in monitoraggi:
            counters["processed"] += 1

            try:
                user = m.abbonamento.utente
                perf = m.performance

                if perf is None:
                    counters["skip_no_perf"] += 1
                    if verbose:
                        self.stdout.write(f"[SKIP] monitoraggio {m.id}: performance mancante")
                    continue

                if _has_internal_tickets(perf):
                    counters["skip_internal"] += 1
                    if verbose:
                        self.stdout.write(f"[SKIP] perf {perf.id}: biglietti già presenti (DB/listing)")
                    continue

                fs_url, mapping_type, mapping_pk = _get_fansale_mapping_for_performance(perf)
                if not fs_url:
                    counters["skip_no_mapping"] += 1
                    if verbose:
                        self.stdout.write(f"[SKIP] perf {perf.id}: mapping fanSALE assente")
                    continue

                if _is_recently_soft_blocked(mapping_type, mapping_pk, minutes=soft_block_minutes):
                    counters["skip_soft_blocked"] += 1
                    if verbose:
                        self.stdout.write(f"[SKIP] perf {perf.id}: mapping fanSALE in soft-block recente")
                    continue

                if verbose:
                    self.stdout.write(f"[CHECK] perf {perf.id} url={fs_url}")

                result = check_fansale_page_availability_selenium(
                    url=fs_url,
                    wait_s=selenium_wait,
                    page_load_timeout=page_timeout,
                    firefox_binary=firefox_binary,
                )

                _touch_last_scan(mapping_type, mapping_pk)
                _update_mapping_snapshot(mapping_type, mapping_pk, result)

                if not result.get("ok"):
                    counters["fansale_error"] += 1
                    blocked_events += 1

                    if verbose:
                        self.stdout.write(
                            f"[FANSALE ERR] perf {perf.id} "
                            f"status={result.get('status_code')} reason={result.get('reason')}"
                        )

                    if blocked_events >= max_blocked:
                        self.stdout.write(
                            f"[STOP] troppi segnali di blocco/errore su fanSALE ({blocked_events}). "
                            f"Interrompo il job per prudenza."
                        )
                        break

                    _sleep_with_jitter_fansale(sleep_s, heavy=True)
                    continue

                availability = result.get("availability")

                if availability == "unknown":
                    counters["skip_unknown"] += 1
                    if verbose:
                        self.stdout.write(f"[FANSALE] perf {perf.id} => unknown ({result.get('reason')})")
                    _sleep_with_jitter_fansale(sleep_s)
                    continue

                if availability != "available":
                    counters["skip_not_avail"] += 1
                    if verbose:
                        self.stdout.write(f"[FANSALE] perf {perf.id} => {availability} ({result.get('reason')})")
                    _sleep_with_jitter_fansale(sleep_s)
                    continue

                dk = _dedupe_key(perf.id, user.id, "fansale", "BACK_IN_STOCK")
                if Notifica.objects.filter(dedupe_key=dk, status="SENT").exists():
                    counters["skip_dedup"] += 1
                    if verbose:
                        self.stdout.write(f"[DEDUP] perf {perf.id} già notificata oggi (SENT)")
                    _sleep_with_jitter_fansale(sleep_s)
                    continue

                if not getattr(user, "notify_email", True):
                    counters["no_email_pref"] += 1
                    if verbose:
                        self.stdout.write(f"[NO EMAIL PREF] user={getattr(user, 'email', None)}")
                    _sleep_with_jitter_fansale(sleep_s)
                    continue

                event_title = perf.evento.nome_evento if getattr(perf, "evento_id", None) else "Evento"
                luogo = getattr(perf.luogo, "nome", "") if getattr(perf, "luogo_id", None) else ""
                when = perf.starts_at_utc.isoformat() if getattr(perf, "starts_at_utc", None) else "—"

                subject = f"Biglietti disponibili su fanSALE: {event_title}"
                msg = (
                    f"Ciao {getattr(user, 'first_name', '')},\n\n"
                    f"Sono stati trovati biglietti disponibili su fanSALE per:\n"
                    f"- Evento: {event_title}\n"
                    f"- Luogo: {luogo}\n"
                    f"- Data: {when}\n\n"
                    f"Link: {fs_url}\n\n"
                    f"— Tixy"
                )

                if dry_run:
                    self.stdout.write(f"[DRY] WOULD EMAIL user={user.email} perf={perf.id} url={fs_url}")
                    counters["notified"] += 1
                    _sleep_with_jitter_fansale(sleep_s)
                    continue

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
                    _sleep_with_jitter_fansale(sleep_s)
                    continue

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

                _sleep_with_jitter_fansale(sleep_s)

            except Exception as ex:
                counters["fansale_error"] += 1
                blocked_events += 1

                self.stdout.write(f"[FATAL-SKIP] monitoraggio={getattr(m, 'id', None)} err={ex}")

                if blocked_events >= max_blocked:
                    self.stdout.write(
                        f"[STOP] troppi errori/blocchi su fanSALE ({blocked_events}). "
                        f"Interrompo il job per prudenza."
                    )
                    break

                _sleep_with_jitter_fansale(sleep_s, heavy=True)
                continue

        self.stdout.write(
            "[DONE] "
            f"processed={counters['processed']} "
            f"notified={counters['notified']} "
            f"skip_no_perf={counters['skip_no_perf']} "
            f"skip_internal={counters['skip_internal']} "
            f"skip_no_mapping={counters['skip_no_mapping']} "
            f"skip_not_avail={counters['skip_not_avail']} "
            f"skip_unknown={counters['skip_unknown']} "
            f"skip_dedup={counters['skip_dedup']} "
            f"fansale_error={counters['fansale_error']} "
            f"email_fail={counters['email_fail']} "
            f"no_email_pref={counters['no_email_pref']} "
            f"skip_soft_blocked={counters['skip_soft_blocked']} "
        )