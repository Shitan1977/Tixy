from __future__ import annotations

import hashlib
import time
from typing import Any, Dict, Optional, List

from django.core.management.base import BaseCommand
from django.core.mail import send_mail
from django.db import transaction
from django.utils import timezone
from django.db.models import Q
from api.models import Piattaforma, EventoPiattaforma, Notifica, Monitoraggio

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


class Command(BaseCommand):
    help = "Scan Ticketmaster resale (separato): rileva rivendite, dedupe, crea Notifica e invia email."

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
        # email ON di default (come richiesto), ma puoi spegnerla se serve
        parser.add_argument("--no-email", action="store_true", help="Non inviare email (crea solo Notifica o aggiorna snapshot)")

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

        qs = (
            EventoPiattaforma.objects
            .filter(piattaforma=plat)
            .exclude(url__isnull=True).exclude(url="")
            .order_by("-id")[:limit]
        )

        self.stdout.write(self.style.SUCCESS(
            f"[RESALE] START now={now.isoformat()} count={qs.count()} dry_run={dry_run} enable_prices={enable_prices} email={'OFF' if no_email else 'ON'}"
        ))

        found = 0
        updated = 0
        skipped = 0
        errors = 0
        emails_sent = 0
        notif_created = 0
        notif_deduped = 0

        for ep in qs:
            url = ep.url
            event_id_candidate = (ep.id_evento_piattaforma or "").strip()

            try:
                html_res = check_ticketmaster_page_availability(
                    url=url,
                    timeout=timeout,
                    session=None,
                    max_retries=max_retries,
                )

                if enable_prices and event_id_candidate:
                    price_res = fetch_tm_eu_prices(
                        event_id=event_id_candidate,
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

                snapshot = _safe_dict(ep.snapshot_raw)
                prev_checksum = str(snapshot.get("resale_checksum") or "").strip()

                # SKIP (ma aggiorna ultima_scansione)
                if prev_checksum == checksum_now:
                    if not dry_run:
                        EventoPiattaforma.objects.filter(id=ep.id).update(ultima_scansione=now)
                    skipped += 1
                    if verbose:
                        self.stdout.write(f"[RESALE] SKIP same_checksum id={ep.id} avail={combined.availability} resale={combined.is_resale}")
                    if sleep_s > 0:
                        time.sleep(sleep_s)
                    continue

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

                is_found = bool(combined.is_resale and combined.availability == "available")

                if dry_run:
                    updated += 1
                    if is_found:
                        found += 1
                        self.stdout.write(self.style.WARNING(f"[RESALE][DRY] FOUND id={ep.id} url={url}"))
                    else:
                        if verbose:
                            self.stdout.write(f"[RESALE][DRY] UPDATE id={ep.id} avail={combined.availability} resale={combined.is_resale}")
                    if sleep_s > 0:
                        time.sleep(sleep_s)
                    continue

                with transaction.atomic():
                    ep.snapshot_raw = snapshot
                    ep.ultima_scansione = now
                    ep.save(update_fields=["snapshot_raw", "ultima_scansione", "aggiornato_il"])
                    updated += 1

                    if is_found:
                        found += 1
                        self.stdout.write(self.style.SUCCESS(f"[RESALE] FOUND id={ep.id} url={url}"))

                        # 1) Trova monitoraggi interessati
                        monitoraggi = _find_monitoraggi_for_evento_piattaforma(ep)
                        if not monitoraggi:
                            if verbose:
                                self.stdout.write(self.style.WARNING(f"[RESALE] no monitoraggi for ep_id={ep.id}"))
                        for mon in monitoraggi:
                            recipient = _get_user_email_from_monitoraggio(mon)
                            if not recipient:
                                if verbose:
                                    self.stdout.write(self.style.WARNING(f"[RESALE] no recipient for monitoraggio={mon.id}"))
                                continue

                            # 2) dedupe per notifica inviata
                            dk = f"tm_resale:{mon.id}:{checksum_now}"
                            if Notifica.objects.filter(dedupe_key=dk, status="SENT").exists():
                                notif_deduped += 1
                                if verbose:
                                    self.stdout.write(f"[RESALE] DEDUPE monitoraggio={mon.id}")
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
                                msg_lines.append(f"Prezzo: {combined.min_price} - {combined.max_price} {combined.currency or ''}".strip())
                            message = "\n".join(msg_lines)

                            # 3) invio email (stesso backend/settings del progetto)
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
                                    emails_sent += 1
                            except Exception as ex:
                                status = "FAILED"
                                if verbose:
                                    self.stdout.write(self.style.ERROR(f"[RESALE] email FAILED mon={mon.id} to={recipient} ex={ex}"))

                            # 4) salva Notifica SEMPRE (SENT/FAILED)
                            Notifica.objects.create(
                                monitoraggio=mon,
                                channel="email",
                                dedupe_key=dk,
                                status=status,
                                message=message,
                            )
                            notif_created += 1

                    else:
                        if verbose:
                            self.stdout.write(f"[RESALE] UPDATE id={ep.id} avail={combined.availability} resale={combined.is_resale}")

                if sleep_s > 0:
                    time.sleep(sleep_s)

            except Exception as ex:
                errors += 1
                self.stdout.write(self.style.ERROR(f"[RESALE] ERROR id={getattr(ep,'id',None)} url={url} ex={ex}"))
                if sleep_s > 0:
                    time.sleep(sleep_s)
                continue

        self.stdout.write(self.style.SUCCESS(
            f"[RESALE] END found={found} updated={updated} skipped={skipped} errors={errors} "
            f"emails_sent={emails_sent} notifica_created={notif_created} notifica_deduped={notif_deduped}"
        ))