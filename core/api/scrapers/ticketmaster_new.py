
"""
Ticketmaster Discovery API v2 - collector "completo" per IT
Obiettivo: prendere TUTTI gli eventi (e tutte le date) che si terranno in Italia,
anche se NON sono ancora in vendita / non hanno biglietti disponibili.

NOTE IMPORTANTI (senza inventare):
- Ticketmaster Discovery API ha un limite di "deep paging": non puoi ottenere "tutto IT" in una singola query.
  Soluzione: windowing per intervalli di tempo (mese/settimana).
- Alcuni eventi hanno localDate ma NON dateTime (TBA/TBD o orario non pubblicato).
  Lo script li include comunque; l'importer deciderà come persisterli.

Richiede env var: TICKETMASTER_API_KEY
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
    # sha256 "inline" senza import extra per evitare dipendenze? no: usiamo hashlib
    import hashlib
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class TMWindow:
    start: datetime
    end: datetime


def fetch_events_page(
    *,
    page: int = 0,
    size: int = 195,  # 195 per stare larghi e ridurre edge/ultima pagina
    country_code: str = "IT",
    startDateTime: Optional[str] = None,
    endDateTime: Optional[str] = None,
    include_tba: bool = True,
    include_tbd: bool = True,
    source: Optional[str] = None,
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

    for attempt in range(max_retries_429 + 1):
        r = requests.get(TM_BASE, params=params, timeout=timeout)

        # rate limit
        if r.status_code == 429 and attempt < max_retries_429:
            retry_after = r.headers.get("Retry-After")
            sleep_s = int(retry_after) if (retry_after and retry_after.isdigit()) else (2 ** attempt)
            time.sleep(sleep_s)
            continue

        r.raise_for_status()
        if r.status_code == 400:
            raise TicketmasterError(f"400 Bad Request\nURL: {r.url}\nBODY: {r.text[:600]}")

        return r.json()

    raise TicketmasterError("Too many 429 responses from Ticketmaster")


def iter_events_in_window(
    *,
    window: TMWindow,
    country_code: str = "IT",
    size: int = 195,
    include_tba: bool = True,
    include_tbd: bool = True,
    source: Optional[str] = None,
    hard_page_cap: int = 20_000,
) -> Iterator[Dict[str, Any]]:
    """
    Itera tutti gli eventi in una finestra temporale.
    La finestra serve per evitare il limite di deep paging (1000 item).
    """
    page = 0
    total_pages: Optional[int] = None

    start_str = iso_z(window.start)
    end_str = iso_z(window.end)

    while total_pages is None or page < total_pages:
        if page >= hard_page_cap:
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

        embedded = data.get("_embedded") or {}
        events = embedded.get("events") or []

        for e in events:
            yield e

        page_info = data.get("page") or {}
        total_pages = page_info.get("totalPages")

        # fallback: se non c’è, esci quando non arrivano risultati
        if total_pages is None and not events:
            break

        page += 1


def build_windows(
    *,
    start_utc: datetime,
    end_utc: datetime,
    step_days: int = 30,
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
    step_days: int = 30,
    size: int = 195,
    include_tba: bool = True,
    include_tbd: bool = True,
    source: Optional[str] = None,
) -> Iterator[Dict[str, Any]]:
    """
    Itera TUTTI gli eventi in IT nel range definito, spezzando per finestre.

    Default: da "adesso" a +18 mesi.
    """
    now = datetime.now(timezone.utc)

    if start_utc is None:
        start_utc = now
    if end_utc is None:
        # +months_ahead approx (senza dipendenze): 30 giorni * months
        end_utc = now + timedelta(days=30 * months_ahead)

    windows = build_windows(start_utc=start_utc, end_utc=end_utc, step_days=step_days)

    # Dedup globale su ID: un evento può apparire su finestre adiacenti.
    seen_ids: set[str] = set()

    for w in windows:
        for e in iter_events_in_window(
            window=w,
            country_code=country_code,
            size=size,
            include_tba=include_tba,
            include_tbd=include_tbd,
            source=source,
        ):
            eid = e.get("id")
            if not eid:
                continue
            if eid in seen_ids:
                continue
            seen_ids.add(eid)
            yield e
