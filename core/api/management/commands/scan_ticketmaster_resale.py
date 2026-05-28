from __future__ import annotations

import hashlib
import time
from typing import Any, Dict, Optional, List

from django.core.management.base import BaseCommand
from django.core.mail import send_mail
from django.db import transaction
from django.utils import timezone
from django.db.models import Q
from api.models import Piattaforma, EventoPiattaforma, PerformancePiattaforma, Notifica, Monitoraggio

# Riusa il "probe" già testato e funzionante (NO duplicazione logica)
from api.management.commands.ticketmaster_resale import (
    check_ticketmaster_page_availability,
    fetch_tm_eu_prices,
    merge_tm_signals,
    PriceResult,
)


def sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _safe_dict(obj: Any) -> Dict[str, Any]:
    return obj if isinstance(obj, dict) else {}


def _get_user_email_from_monitoraggio(mon: Any) -> Optional[str]:
    """
    Estrae email utente in modo robusto, senza conoscere esattamente la struttura.
    Prova i path più comuni:
      mon.abbonamento.utente.email
      mon.abbonamento.user.email
      mon.utente.email
      mon.user.email
    """
    for path in (
        ("abbonamento", "utente", "email"),
        ("abbonamento", "user", "email"),
        ("utente", "email"),
        ("user", "email"),
    ):
        cur = mon
        ok = True
        for attr in path:
            if not hasattr(cur, attr):
                ok = False
                break
            cur = getattr(cur, attr)
            if cur is None:
                ok = False
                break
        if ok and isinstance(cur, str) and "@" in cur:
            return cur.strip()
    return None


def _find_monitoraggi_for_evento_piattaforma(ep: EventoPiattaforma):
    """
    Monitoraggio collega EVENTO o PERFORMANCE.
    Per Ticketmaster, EventoPiattaforma collega l'Evento, quindi:
      - monitoraggi su evento = ep.evento
      - monitoraggi su performance il cui evento = ep.evento
    """
    if not getattr(ep, "evento_id", None):
        return []

    return list(
        Monitoraggio.objects.filter(
            Q(evento_id=ep.evento_id) | Q(performance__evento_id=ep.evento_id)
        ).select_related("abbonamento")
    )


def _find_monitoraggi_for_performance_piattaforma(pp: PerformancePiattaforma):
    """
    Cerca monitoraggi collegati a una PerformancePiattaforma, tramite:
      - monitoraggi su performance = pp.performance
      - monitoraggi su evento = pp.performance.evento
    """
    performance_id = getattr(pp, "performance_id", None)
    if not performance_id:
        return []

    # Prova a risalire all'evento tramite la performance
    evento_id = None
    try:
        evento_id = pp.performance.evento_id
    except Exception:
        pass

    q = Q(performance_id=performance_id)
    if evento_id:
        q |= Q(evento_id=evento_id)

    return list(
        Monitoraggio.objects.filter(q).select_related("abbonamento")
    )


def _process_url(
    *,
    record_id: int,
    url: str,
    id_evento_piattaforma: str,
    snapshot_getter,       # callable() -> dict
    snapshot_setter,       # callable(dict) -> None  (solo update in-memory, il save avviene fuori)
    save_snapshot,         # callable(now) -> None   (esegue il DB save)
    find_monitoraggi,      # callable() -> list[Monitoraggio]
    now,
    timeout: int,
    max_retries: int,
    domain: str,
    lang: str,
    enable_prices: bool,
    dry_run: bool,
    verbose: bool,
    no_email: bool,
    stdout,
    style,
    sleep_s: float,
) -> dict:
    """
    Logica di probe + dedupe + notifica estratta in funzione riusabile
    per EventoPiattaforma e PerformancePiattaforma.

    Restituisce un dict con i contatori da sommare al chiamante:
      found, updated, skipped, errors, emails_sent, notif_created, notif_deduped
    """
    counters = dict(found=0, updated=0, skipped=0, errors=0,
                    emails_sent=0, notif_created=0, notif_deduped=0)

    try:
        html_res = check_ticketmaster_page_availability(
            url=url,
            timeout=timeout,
            session=None,
            max_retries=max_retries,
        )

        if enable_prices and id_evento_piattaforma:
            price_res = fetch_tm_eu_prices(
                event_id=id_evento_piattaforma,
                domain=domain,
                lang=lang,
            )
        else:
            price_res = PriceResult(
                ok=False,
                status_code=None,
                availability="unknown",
                min_price=None,
                max_price=None,
                currency=None,
                reason="prices skipped",
                raw=None,
            )

        combined = merge_tm_signals(html_res, price_res)

        final_url = None
        if combined.html and isinstance(combined.html, dict):
            final_url = combined.html.get("final_url")

        checksum_now = sha256(f"{combined.availability}|{combined.is_resale}|{final_url or url}")

        snapshot = snapshot_getter()
        prev_checksum = str(snapshot.get("resale_checksum") or "").strip()

        # SKIP (ma aggiorna ultima_scansione)
        if prev_checksum == checksum_now:
            if not dry_run:
                save_snapshot(now)
            counters["skipped"] += 1
            if verbose:
                stdout.write(f"[RESALE] SKIP same_checksum id={record_id} avail={combined.availability} resale={combined.is_resale}")
            if sleep_s > 0:
                time.sleep(sleep_s)
            return counters

        # aggiorna snapshot
        snapshot["resale_probe"] = {
            "ok": combined.ok,
            "availability": combined.availability,
            "is_resale": combined.is_resale,
            "min_price": combined.min_price,
            "max_price": combined.max_price,
            "currency": combined.currency,
            "source": combined.source,
            "reason": combined.reason,
            "html": combined.html,
            "prices": combined.prices,
            "scanned_at": now.isoformat(),
        }
        snapshot["resale_checksum"] = checksum_now
        snapshot_setter(snapshot)

        is_found = bool(combined.is_resale and combined.availability == "available")

        if dry_run:
            counters["updated"] += 1
            if is_found:
                counters["found"] += 1
                stdout.write(style.WARNING(f"[RESALE][DRY] FOUND id={record_id} url={url}"))
            else:
                if verbose:
                    stdout.write(f"[RESALE][DRY] UPDATE id={record_id} avail={combined.availability} resale={combined.is_resale}")
            if sleep_s > 0:
                time.sleep(sleep_s)
            return counters

        with transaction.atomic():
            save_snapshot(now)
            counters["updated"] += 1

            if is_found:
                counters["found"] += 1
                stdout.write(style.SUCCESS(f"[RESALE] FOUND id={record_id} url={url}"))

                monitoraggi = find_monitoraggi()
                if not monitoraggi:
                    if verbose:
                        stdout.write(style.WARNING(f"[RESALE] no monitoraggi for id={record_id}"))

                for mon in monitoraggi:
                    recipient = _get_user_email_from_monitoraggio(mon)
                    if not recipient:
                        if verbose:
                            stdout.write(style.WARNING(f"[RESALE] no recipient for monitoraggio={mon.id}"))
                        continue

                    dk = f"tm_resale:{mon.id}:{checksum_now}"
                    if Notifica.objects.filter(dedupe_key=dk, status="SENT").exists():
                        counters["notif_deduped"] += 1
                        if verbose:
                            stdout.write(f"[RESALE] DEDUPE monitoraggio={mon.id}")
                        continue

                    subject = "[Tixy] Ticketmaster: Rivendita disponibile"
                    msg_lines = [
                        "RIVENDITA DISPONIBILE su Ticketmaster",
                        "",
                        f"URL: {final_url or url}",
                        f"Disponibilità: {combined.availability}",
                        f"Rivendita: {combined.is_resale}",
                    ]
                    if combined.min_price is not None or combined.max_price is not None:
                        msg_lines.append(
                            f"Prezzo: {combined.min_price} - {combined.max_price} {combined.currency or ''}".strip()
                        )
                    message = "\n".join(msg_lines)

                    status = "SENT"
                    try:
                        if not no_email:
                            send_mail(
                                subject=subject,
                                message=message,
                                from_email=None,  # usa DEFAULT_FROM_EMAIL
                                recipient_list=[recipient],
                                fail_silently=False,
                            )
                            counters["emails_sent"] += 1
                    except Exception as ex:
                        status = "FAILED"
                        if verbose:
                            stdout.write(style.ERROR(
                                f"[RESALE] email FAILED mon={mon.id} to={recipient} ex={ex}"
                            ))

                    Notifica.objects.create(
                        monitoraggio=mon,
                        channel="email",
                        dedupe_key=dk,
                        status=status,
                        message=message,
                    )
                    counters["notif_created"] += 1

            else:
                if verbose:
                    stdout.write(f"[RESALE] UPDATE id={record_id} avail={combined.availability} resale={combined.is_resale}")

        if sleep_s > 0:
            time.sleep(sleep_s)

    except Exception as ex:
        counters["errors"] += 1
        stdout.write(style.ERROR(f"[RESALE] ERROR id={record_id} url={url} ex={ex}"))
        if sleep_s > 0:
            time.sleep(sleep_s)

    return counters


def _sum_counters(a: dict, b: dict) -> dict:
    return {k: a[k] + b[k] for k in a}


def _get_pp_event_id(pp: PerformancePiattaforma) -> str:
    """
    Ricava l'event_id Ticketmaster da PerformancePiattaforma con questa priorità:

    1. pp.snapshot_raw["id"]          → es. "Z59rmIYnyZ7a1"  (già pulito)
    2. pp.external_perf_id            → es. "Z59rmIYnyZ7A1-perf-285"
       → taglia tutto da "-perf-" in poi

    Restituisce stringa vuota se non trovato.
    """
    # 1. snapshot_raw["id"]
    snap = _safe_dict(getattr(pp, "snapshot_raw", None))
    snap_id = str(snap.get("id") or "").strip()
    if snap_id:
        return snap_id

    # 2. external_perf_id con strip di "-perf-..."
    ext = str(getattr(pp, "external_perf_id", "") or "").strip()
    if ext:
        # taglia "-perf-<qualsiasi cosa>" (case-insensitive)
        import re
        cleaned = re.split(r"-perf-", ext, maxsplit=1, flags=re.IGNORECASE)[0].strip()
        if cleaned:
            return cleaned

    return ""


class Command(BaseCommand):
    help = (
        "Scan Ticketmaster resale: rileva rivendite su EventoPiattaforma "
        "E PerformancePiattaforma, dedupe, crea Notifica e invia email."
    )

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=200)
        parser.add_argument("--sleep", type=float, default=0.0)
        parser.add_argument("--timeout", type=int, default=20)
        parser.add_argument("--max-retries", type=int, default=4)
        parser.add_argument("--domain", default="it")
        parser.add_argument("--lang", default="it")
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--verbose", action="store_true")
        parser.add_argument("--enable-prices", action="store_true")
        parser.add_argument(
            "--no-email",
            action="store_true",
            help="Non inviare email (crea solo Notifica o aggiorna snapshot)",
        )

    def handle(self, *args, **opt):
        limit = int(opt["limit"])
        sleep_s = float(opt["sleep"])
        timeout = int(opt["timeout"])
        max_retries = int(opt["max_retries"])
        domain = str(opt["domain"])
        lang = str(opt["lang"])
        dry_run = bool(opt["dry_run"])
        verbose = bool(opt["verbose"])
        enable_prices = bool(opt["enable_prices"])
        no_email = bool(opt["no_email"])

        now = timezone.now()

        plat = Piattaforma.objects.filter(nome__iexact="ticketmaster").first()
        if not plat:
            self.stdout.write(self.style.ERROR("[RESALE] Piattaforma 'ticketmaster' non trovata in DB."))
            return

        # ── QuerySet 1: EventoPiattaforma (logica originale) ──────────────────
        ep_qs = (
            EventoPiattaforma.objects
            .filter(piattaforma=plat)
            .exclude(url__isnull=True).exclude(url="")
            .order_by("-id")[:limit]
        )

        # ── QuerySet 2: PerformancePiattaforma (NUOVO) ────────────────────────
        pp_qs = (
            PerformancePiattaforma.objects
            .filter(piattaforma=plat)
            .exclude(url__isnull=True).exclude(url="")
            .order_by("-id")[:limit]
        )

        self.stdout.write(self.style.SUCCESS(
            f"[RESALE] START now={now.isoformat()} "
            f"ep_count={ep_qs.count()} pp_count={pp_qs.count()} "
            f"dry_run={dry_run} enable_prices={enable_prices} "
            f"email={'OFF' if no_email else 'ON'}"
        ))

        totals = dict(found=0, updated=0, skipped=0, errors=0,
                      emails_sent=0, notif_created=0, notif_deduped=0)

        shared_kwargs = dict(
            now=now,
            timeout=timeout,
            max_retries=max_retries,
            domain=domain,
            lang=lang,
            enable_prices=enable_prices,
            dry_run=dry_run,
            verbose=verbose,
            no_email=no_email,
            stdout=self.stdout,
            style=self.style,
            sleep_s=sleep_s,
        )

        # ── Loop 1: EventoPiattaforma ─────────────────────────────────────────
        self.stdout.write("[RESALE] --- EventoPiattaforma ---")
        for ep in ep_qs:
            url = ep.url
            event_id_candidate = (ep.id_evento_piattaforma or "").strip()

            def _ep_snapshot_getter(ep=ep):
                return _safe_dict(ep.snapshot_raw)

            def _ep_snapshot_setter(snap, ep=ep):
                ep.snapshot_raw = snap

            def _ep_save_snapshot(ts, ep=ep):
                EventoPiattaforma.objects.filter(id=ep.id).update(
                    snapshot_raw=ep.snapshot_raw,
                    ultima_scansione=ts,
                )

            def _ep_find_monitoraggi(ep=ep):
                return _find_monitoraggi_for_evento_piattaforma(ep)

            c = _process_url(
                record_id=ep.id,
                url=url,
                id_evento_piattaforma=event_id_candidate,
                snapshot_getter=_ep_snapshot_getter,
                snapshot_setter=_ep_snapshot_setter,
                save_snapshot=_ep_save_snapshot,
                find_monitoraggi=_ep_find_monitoraggi,
                **shared_kwargs,
            )
            totals = _sum_counters(totals, c)

        # ── Loop 2: PerformancePiattaforma ────────────────────────────────────
        self.stdout.write("[RESALE] --- PerformancePiattaforma ---")
        for pp in pp_qs:
            url = pp.url

            # Ricava event_id Ticketmaster:
            #   1) snapshot_raw["id"]        (già pulito, priorità massima)
            #   2) external_perf_id          (taglia "-perf-..." se presente)
            event_id_candidate = _get_pp_event_id(pp)
            if verbose:
                self.stdout.write(
                    f"[RESALE] PP id={pp.id} event_id_candidate={event_id_candidate!r}"
                )

            def _pp_snapshot_getter(pp=pp):
                return _safe_dict(pp.snapshot_raw)

            def _pp_snapshot_setter(snap, pp=pp):
                pp.snapshot_raw = snap

            def _pp_save_snapshot(ts, pp=pp):
                PerformancePiattaforma.objects.filter(id=pp.id).update(
                    snapshot_raw=pp.snapshot_raw,
                    ultima_scansione=ts,
                )

            def _pp_find_monitoraggi(pp=pp):
                return _find_monitoraggi_for_performance_piattaforma(pp)

            c = _process_url(
                record_id=pp.id,
                url=url,
                id_evento_piattaforma=event_id_candidate,
                snapshot_getter=_pp_snapshot_getter,
                snapshot_setter=_pp_snapshot_setter,
                save_snapshot=_pp_save_snapshot,
                find_monitoraggi=_pp_find_monitoraggi,
                **shared_kwargs,
            )
            totals = _sum_counters(totals, c)

        self.stdout.write(self.style.SUCCESS(
            f"[RESALE] END "
            f"found={totals['found']} "
            f"updated={totals['updated']} "
            f"skipped={totals['skipped']} "
            f"errors={totals['errors']} "
            f"emails_sent={totals['emails_sent']} "
            f"notifica_created={totals['notif_created']} "
            f"notifica_deduped={totals['notif_deduped']}"
        ))