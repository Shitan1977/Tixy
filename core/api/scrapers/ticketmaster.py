# api/scrapers/ticketmaster.py
import os
import time
import requests
from typing import Dict, Iterator, Any, Optional

TM_BASE = "https://app.ticketmaster.com/discovery/v2/events.json"

class TicketmasterError(RuntimeError):
    pass

def _get_api_key() -> str:
    api_key = os.getenv("TICKETMASTER_API_KEY")
    if not api_key:
        raise TicketmasterError("Missing env var TICKETMASTER_API_KEY")
    return api_key

def fetch_events_page(
    *,
    page: int = 0,
    size: int = 200,
    country_code: str = "IT",
    startDateTime: Optional[str] = None,  # ISO 8601, es: "2026-01-01T00:00:00Z"
    endDateTime: Optional[str] = None,
    include_tba: bool = True,
    include_tbd: bool = True,
    source: Optional[str] = None,  # es: "ticketmaster,universe,frontgate,tmr"
    sort: str = "date,asc",
    timeout: int = 25,
    max_retries_429: int = 5,
) -> Dict[str, Any]:
    api_key = _get_api_key()

    params = {
        "apikey": api_key,
        "countryCode": country_code,
        "page": page,
        "size": size,
        "sort": sort,
    }
    if startDateTime:
        params["startDateTime"] = startDateTime
    if endDateTime:
        params["endDateTime"] = endDateTime

    # “tutto” = includi anche roba TBD/TBA (se la tua app la vuole)
    params["includeTBA"] = "yes" if include_tba else "no"
    params["includeTBD"] = "yes" if include_tbd else "no"

    # di default è “all sources”, ma puoi esplicitarlo
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
        return r.json()

    raise TicketmasterError("Too many 429 responses from Ticketmaster")

def iter_all_events(
    *,
    country_code: str = "IT",
    size: int = 200,
    startDateTime: Optional[str] = None,
    endDateTime: Optional[str] = None,
    include_tba: bool = True,
    include_tbd: bool = True,
    source: Optional[str] = None,
    hard_page_cap: int = 2000,  # safety
) -> Iterator[Dict[str, Any]]:
    page = 0
    total_pages = None

    while total_pages is None or page < total_pages:
        if page >= hard_page_cap:
            break

        data = fetch_events_page(
            page=page,
            size=size,
            country_code=country_code,
            startDateTime=startDateTime,
            endDateTime=endDateTime,
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
        if total_pages is None:
            # fallback: se non c’è, esci quando non arrivano risultati
            if not events:
                break

        page += 1
