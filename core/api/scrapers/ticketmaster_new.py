"""
Ticketmaster Discovery API v2 - collector "completo" per IT
Obiettivo: prendere TUTTI gli eventi (e tutte le date) che si terranno in Italia,
anche se NON sono ancora in vendita / non hanno biglietti disponibili.

NOTE IMPORTANTI (senza inventare):
- Ticketmaster Discovery API ha un limite di "deep paging": non puoi ottenere "tutto IT" in una singola query.
  Soluzione: windowing per intervalli di tempo (mese/settimana).
- Alcuni eventi hanno localDate ma NON dateTime (TBA/TBD o orario non pubblicato).
  Lo script li include comunque; l'importer deciderà come persisterli.
- Deep paging hard limit TM: max 5 pagine per finestra (5 × size = max ~975 elementi).
  Se totalPages > 5, la finestra è troppo larga: ridurre step_days.

Richiede env var: TICKETMASTER_API_KEY

PATCH:
  A - sleep tra finestre consecutive per ridurre pressione rate limit
  B - hard_page_cap abbassato a 5 (limite reale TM); warning se finestra troppo larga
  C - Retry-After parsing robusto (float invece di isdigit)
  D - iter_all_events_windowed resiliente ai 429: finestra fallita loggata + pausa lunga + continua
"""

from __future__ import annotations

import os
import time
import json
import requests
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterator, Optional, List

TM_BASE = "https://app.ticketmaster.com/discovery/v2/events.json"

# PATCH B — limite reale di deep paging Ticketmaster.
# Oltre pagina 4 (0-indexed) la risposta è vuota o 400.
TM_DEEP_PAGING_MAX_PAGES = 5

# PATCH A — pausa minima tra finestre consecutive (secondi).
# Ticketmaster misura le chiamate al minuto, non solo per singola richiesta.
TM_INTER_WINDOW_SLEEP_S = 0.5

# PATCH resilienza 429 — pausa lunga dopo una finestra fallita per rate limit.
# 60 secondi lasciano raffreddare il quota TM prima di riprovare la finestra successiva.
TM_RATE_LIMIT_BACKOFF_S = 60


class TicketmasterError(RuntimeError):
    pass


def _get_api_key() -> str:
    api_key = os.getenv("TICKETMASTER_API_KEY")
    if not api_key:
        raise TicketmasterError("Missing env var TICKETMASTER_API_KEY")
    return api_key


def iso_z(dt: datetime) -> str:
    """
    Ticketmaster-friendly ISO UTC:
    - no microseconds
    - always Z
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc).replace(microsecond=0)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_dt_utc(dt_str: Optional[str]) -> Optional[datetime]:
    if not dt_str:
        return None
    return datetime.fromisoformat(dt_str.replace("Z", "+00:00")).astimezone(timezone.utc)


def stable_checksum(obj: Any) -> str:
    """
    JSON stable representation. Non usa str(obj) per evitare checksum instabili.
    """
    s = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    import hashlib
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class TMWindow:
    start: datetime
    end: datetime


def fetch_events_page(
    *,
    page: int = 0,
    size: int = 195,
    country_code: str = "IT",
    startDateTime: Optional[str] = None,
    endDateTime: Optional[str] = None,
    include_tba: bool = True,
    include_tbd: bool = True,
    source: Optional[str] = None,
    keyword: Optional[str] = None,
    sort: str = "date,asc",
    timeout: int = 25,
    max_retries_429: int = 6,
) -> Dict[str, Any]:
    api_key = _get_api_key()

    params: Dict[str, Any] = {
        "apikey": api_key,
        "countryCode": country_code,
        "page": page,
        "size": size,
        "sort": sort,
        "includeTBA": "yes" if include_tba else "no",
        "includeTBD": "yes" if include_tbd else "no",
    }
    if startDateTime:
        params["startDateTime"] = startDateTime
    if endDateTime:
        params["endDateTime"] = endDateTime
    if source:
        params["source"] = source
    if keyword:
        params["keyword"] = keyword

    for attempt in range(max_retries_429 + 1):
        r = requests.get(TM_BASE, params=params, timeout=timeout)

        if r.status_code == 429:
            if attempt < max_retries_429:
                retry_after = r.headers.get("Retry-After")

                # PATCH C — parsing robusto: float() gestisce "1", "1.5", "60.0"
                # isdigit() falliva su stringhe con decimali o spazi.
                if retry_after:
                    try:
                        sleep_s = float(retry_after)
                    except (ValueError, TypeError):
                        sleep_s = float(2 ** attempt)
                else:
                    sleep_s = float(2 ** attempt)

                time.sleep(sleep_s)
                continue
            else:
                # PATCH 1 — ultimo tentativo ancora 429: solleviamo TicketmasterError
                # esplicitamente invece di lasciare che r.raise_for_status() generi
                # un generico requests.HTTPError, difficile da distinguere nei log.
                raise TicketmasterError(
                    f"Too many 429 responses from Ticketmaster (page={page}, attempts={max_retries_429 + 1})"
                )

        if r.status_code == 400:
            raise TicketmasterError(f"400 Bad Request\nURL: {r.url}\nBODY: {r.text[:600]}")

        r.raise_for_status()
        return r.json()


def iter_events_in_window(
    *,
    window: TMWindow,
    country_code: str = "IT",
    size: int = 195,
    include_tba: bool = True,
    include_tbd: bool = True,
    source: Optional[str] = None,
    # PATCH B — hard_page_cap abbassato al limite reale TM (era 20_000, inutile e dannoso).
    hard_page_cap: int = TM_DEEP_PAGING_MAX_PAGES,
    debug_window: bool = False,
) -> Iterator[Dict[str, Any]]:
    page = 0
    total_pages: Optional[int] = None
    printed_header = False

    start_str = iso_z(window.start)
    end_str = iso_z(window.end)

    while total_pages is None or page < total_pages:
        # PATCH B — rispetta il limite reale di deep paging TM.
        if page >= hard_page_cap:
            if debug_window:
                print(
                    f"[TM WINDOW] {start_str} -> {end_str} | "
                    f"deep paging cap raggiunto a page={page} (totalPages={total_pages}). "
                    f"Considera step_days più piccolo se totalPages > {TM_DEEP_PAGING_MAX_PAGES}."
                )
            break

        data = fetch_events_page(
            page=page,
            size=size,
            country_code=country_code,
            startDateTime=start_str,
            endDateTime=end_str,
            include_tba=include_tba,
            include_tbd=include_tbd,
            source=source,
        )

        page_info = data.get("page") or {}
        total_pages = page_info.get("totalPages")
        total_elements = page_info.get("totalElements")

        if debug_window and not printed_header:
            printed_header = True
            # PATCH B — avvisa se la finestra ha più pagine del limite reale.
            warning = ""
            if total_pages is not None and total_pages > TM_DEEP_PAGING_MAX_PAGES:
                warning = (
                    f" ⚠️  FINESTRA TROPPO LARGA: totalPages={total_pages} > "
                    f"cap={TM_DEEP_PAGING_MAX_PAGES}. "
                    f"Ridurre step_days per non perdere eventi."
                )
            print(
                f"[TM WINDOW] {start_str} -> {end_str} | "
                f"totalElements={total_elements} totalPages={total_pages} | "
                f"size={size} | deepPagingCap={hard_page_cap}"
                f"{warning}"
            )

        embedded = data.get("_embedded") or {}
        events = embedded.get("events") or []

        for e in events:
            yield e

        if total_pages is None and not events:
            break

        page += 1


def build_windows(
    *,
    start_utc: datetime,
    end_utc: datetime,
    step_days: int = 14,
) -> List[TMWindow]:
    if start_utc.tzinfo is None:
        start_utc = start_utc.replace(tzinfo=timezone.utc)
    if end_utc.tzinfo is None:
        end_utc = end_utc.replace(tzinfo=timezone.utc)
    start_utc = start_utc.astimezone(timezone.utc)
    end_utc = end_utc.astimezone(timezone.utc)

    if end_utc <= start_utc:
        return []

    step = timedelta(days=step_days)
    out: List[TMWindow] = []
    cur = start_utc
    while cur < end_utc:
        nxt = min(cur + step, end_utc)
        out.append(TMWindow(start=cur, end=nxt))
        cur = nxt
    return out


def iter_all_events_windowed(
    *,
    country_code: str = "IT",
    start_utc: Optional[datetime] = None,
    end_utc: Optional[datetime] = None,
    months_ahead: int = 18,
    step_days: int = 14,
    size: int = 195,
    include_tba: bool = True,
    include_tbd: bool = True,
    source: Optional[str] = None,
) -> Iterator[Dict[str, Any]]:
    """
    Itera TUTTI gli eventi in IT nel range definito, spezzando per finestre.

    Default: da "adesso" a +18 mesi, finestre da 14 giorni.

    PATCH A — aggiunto sleep tra finestre per rispettare rate limit TM.
    PATCH B — hard_page_cap=5 per rispettare il limite reale di deep paging.
    PATCH D — TicketmasterError per singola finestra catturata: pausa lunga e continua.
               Lo scraper non si ferma più con processed=0 al primo 429.
    """
    now = datetime.now(timezone.utc)

    if start_utc is None:
        start_utc = now
    if end_utc is None:
        end_utc = now + timedelta(days=30 * months_ahead)

    windows = build_windows(start_utc=start_utc, end_utc=end_utc, step_days=step_days)

    # Dedup globale su ID: un evento può apparire su finestre adiacenti.
    seen_ids: set[str] = set()

    for w_idx, w in enumerate(windows):
        # PATCH A — pausa tra finestre (non prima della prima).
        if w_idx > 0:
            time.sleep(TM_INTER_WINDOW_SLEEP_S)

        # PATCH resilienza 429 — se una singola finestra fallisce per rate limit,
        # logghiamo, aspettiamo più a lungo e continuiamo con la finestra successiva.
        # Lo scraper non si ferma più con processed=0 al primo 429.
        try:
            for e in iter_events_in_window(
                window=w,
                country_code=country_code,
                size=size,
                include_tba=include_tba,
                include_tbd=include_tbd,
                source=source,
                debug_window=True,
            ):
                eid = e.get("id")
                if not eid:
                    continue
                if eid in seen_ids:
                    continue
                seen_ids.add(eid)
                yield e

        except TicketmasterError as tm_err:
            start_str = iso_z(w.start)
            end_str = iso_z(w.end)
            print(
                f"[TM WINDOW ERROR] finestra {start_str} -> {end_str} fallita: {tm_err}. "
                f"Pausa {TM_RATE_LIMIT_BACKOFF_S}s prima della finestra successiva."
            )
            time.sleep(TM_RATE_LIMIT_BACKOFF_S)


def probe_windows(
    *,
    country_code: str = "IT",
    months_ahead: int = 18,
    step_days: int = 14,
    size: int = 195,
    include_tba: bool = True,
    include_tbd: bool = True,
    source: Optional[str] = None,
):
    """
    Utility di diagnostica: stampa totalElements e totalPages per ogni finestra
    senza scaricare gli eventi. Utile per calibrare step_days.
    """
    now = datetime.now(timezone.utc)
    end_utc = now + timedelta(days=30 * months_ahead)
    windows = build_windows(start_utc=now, end_utc=end_utc, step_days=step_days)

    for w in windows:
        start_str = iso_z(w.start)
        end_str = iso_z(w.end)

        data = fetch_events_page(
            page=0,
            size=size,
            country_code=country_code,
            startDateTime=start_str,
            endDateTime=end_str,
            include_tba=include_tba,
            include_tbd=include_tbd,
            source=source,
        )
        page_info = data.get("page") or {}
        total_pages = page_info.get("totalPages")
        total_elements = page_info.get("totalElements")

        warning = ""
        if total_pages is not None and total_pages > TM_DEEP_PAGING_MAX_PAGES:
            warning = f" ⚠️  TROPPO LARGA (ridurre step_days)"

        print(
            f"[TM PROBE] {start_str} -> {end_str} | "
            f"totalElements={total_elements} totalPages={total_pages} "
            f"size={size}{warning}"
        )