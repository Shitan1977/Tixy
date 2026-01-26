from __future__ import annotations

import time
import requests
from typing import Any, Dict, Optional, Tuple

TM_EU_BASE = "https://app.ticketmaster.eu/mfxapi/v2"
import os

class TicketmasterError(RuntimeError):
    pass

def _get_api_key() -> str:
    api_key = os.getenv("TICKETMASTER_API_KEY")
    if not api_key:
        raise TicketmasterError("Missing env var TICKETMASTER_API_KEY")
    return api_key

def fetch_tm_eu_prices(
    *,
    event_id: str,
    apikey: Optional[str] = None,
    domain: str = "it",
    lang: str = "it",
    timeout: int = 25,
    max_retries_429: int = 6,
) -> Dict[str, Any]:
    """
    Prova a leggere prezzi (e spesso segnali di availability) da Ticketmaster EU (mfxapi).
    Docs: base https://app.ticketmaster.eu/mfxapi/v2/ e apikey in query param. :contentReference[oaicite:1]{index=1}

    Ritorna SEMPRE un dict standard:
    {
      "ok": bool,
      "status_code": int|None,
      "availability": "available"|"limited"|"unavailable"|"unknown",
      "min_price": float|None,
      "max_price": float|None,
      "currency": str|None,
      "reason": str|None,
      "raw": dict|None
    }
    """
    if apikey is None:
        apikey = _get_api_key()  # usa la tua env var TICKETMASTER_API_KEY

    url = f"{TM_EU_BASE}/events/{event_id}/prices"
    params = {
        "apikey": apikey,
        "domain": domain,
        "lang": lang,
    }
    headers = {"Accept": "application/json"}

    last_status = None
    last_text = None

    for attempt in range(max_retries_429 + 1):
        r = requests.get(url, params=params, headers=headers, timeout=timeout)
        last_status = r.status_code
        last_text = r.text[:600] if r.text else ""

        # rate limit
        if r.status_code == 429 and attempt < max_retries_429:
            retry_after = r.headers.get("Retry-After")
            sleep_s = int(retry_after) if (retry_after and retry_after.isdigit()) else (2 ** attempt)
            time.sleep(sleep_s)
            continue

        # casi comuni: id non valido su EU
        if r.status_code == 404:
            return {
                "ok": False,
                "status_code": 404,
                "availability": "unknown",
                "min_price": None,
                "max_price": None,
                "currency": None,
                "reason": "EU mfxapi: event_id non trovato (probabile ID non compatibile)",
                "raw": None,
            }

        # auth/permessi
        if r.status_code in (401, 403):
            return {
                "ok": False,
                "status_code": r.status_code,
                "availability": "unknown",
                "min_price": None,
                "max_price": None,
                "currency": None,
                "reason": f"EU mfxapi: non autorizzato ({r.status_code}). Controllare apikey/permessi.",
                "raw": None,
            }

        # altre 4xx/5xx
        if r.status_code >= 400:
            return {
                "ok": False,
                "status_code": r.status_code,
                "availability": "unknown",
                "min_price": None,
                "max_price": None,
                "currency": None,
                "reason": f"EU mfxapi errore HTTP {r.status_code}: {last_text}",
                "raw": None,
            }

        data = r.json()

        # --- normalizzazione prezzi ---
        min_price, max_price, currency = _extract_min_max_currency_from_prices_payload(data)

        # --- normalizzazione availability ---
        availability = _guess_availability_from_prices_payload(data, min_price, max_price)

        return {
            "ok": True,
            "status_code": r.status_code,
            "availability": availability,
            "min_price": min_price,
            "max_price": max_price,
            "currency": currency,
            "reason": None,
            "raw": data,
        }

    return {
        "ok": False,
        "status_code": last_status,
        "availability": "unknown",
        "min_price": None,
        "max_price": None,
        "currency": None,
        "reason": "Troppi 429 (rate limit) su EU mfxapi",
        "raw": None,
    }


def _extract_min_max_currency_from_prices_payload(data: Any) -> Tuple[Optional[float], Optional[float], Optional[str]]:
    """
    Payload EU /prices può variare: cerchiamo campi numerici sensati.
    Restituisce (min, max, currency) se trovati.
    """
    if not isinstance(data, dict):
        return None, None, None

    candidates = []

    # pattern comuni: liste di prezzi/levels
    for key in ("prices", "priceRanges", "price_range", "offers", "levels"):
        arr = data.get(key)
        if isinstance(arr, list):
            for it in arr:
                if not isinstance(it, dict):
                    continue
                # cerchiamo min/max o value
                for a, b in (("min", "max"), ("minPrice", "maxPrice"), ("min_value", "max_value")):
                    if a in it or b in it:
                        candidates.append(it)
                if "value" in it:
                    candidates.append(it)

    # fallback: scandaglia tutta la dict cercando oggetti con min/max
    # (leggero, ma utile)
    def walk(obj):
        if isinstance(obj, dict):
            yield obj
            for v in obj.values():
                yield from walk(v)
        elif isinstance(obj, list):
            for v in obj:
                yield from walk(v)

    for obj in walk(data):
        if isinstance(obj, dict) and (("min" in obj and "max" in obj) or ("minPrice" in obj and "maxPrice" in obj)):
            candidates.append(obj)

    min_v = None
    max_v = None
    curr = None

    def as_float(x):
        try:
            return float(x)
        except Exception:
            return None

    for it in candidates:
        if not isinstance(it, dict):
            continue
        for ckey in ("currency", "currencyCode", "cur"):
            if it.get(ckey):
                curr = it.get(ckey)
                break

        for a, b in (("min", "max"), ("minPrice", "maxPrice"), ("min_value", "max_value")):
            a_v = as_float(it.get(a))
            b_v = as_float(it.get(b))
            if a_v is not None:
                min_v = a_v if (min_v is None or a_v < min_v) else min_v
            if b_v is not None:
                max_v = b_v if (max_v is None or b_v > max_v) else max_v

        v = as_float(it.get("value"))
        if v is not None:
            min_v = v if (min_v is None or v < min_v) else min_v
            max_v = v if (max_v is None or v > max_v) else max_v

    return min_v, max_v, curr


def _guess_availability_from_prices_payload(data: Any, min_price: Optional[float], max_price: Optional[float]) -> str:
    """
    Heuristica: se payload contiene indicatori di availability usiamoli,
    altrimenti deduciamo: se ci sono prezzi => almeno "limited/available".
    """
    if isinstance(data, dict):
        # indicatori diretti (se presenti)
        for key in ("availability", "status", "onSale", "onsale", "available"):
            if key in data:
                val = data.get(key)
                s = str(val).lower()
                if "sold" in s or "unavail" in s or s in ("false", "0", "no"):
                    return "unavailable"
                if "limit" in s:
                    return "limited"
                if "avail" in s or s in ("true", "1", "yes"):
                    return "available"

    # se abbiamo prezzi, è un segnale positivo (non perfetto ma utile)
    if (min_price is not None) or (max_price is not None):
        return "limited"

    return "unknown"
import re
import requests
from typing import Literal, Optional, Dict, Any

Availability = Literal["available", "unavailable", "unknown"]

def check_ticketmaster_page_availability(*, url: str, timeout: int = 20) -> Dict[str, Any]:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "it-IT,it;q=0.9,en;q=0.7",
        "Referer": "https://www.ticketmaster.it/",
    }

    try:
        r = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
    except Exception as ex:
        return {"ok": False, "availability": "unknown", "status_code": None, "final_url": None, "reason": str(ex)}

    status = r.status_code
    final_url = r.url
    text = (r.text or "").lower()

    # 404 = URL non valido (capita spesso nei nostri dati)
    if status == 404:
        return {"ok": True, "availability": "unknown", "status_code": 404, "final_url": final_url, "reason": "page_404_invalid_url"}

    if status in (401, 403, 429):
        return {"ok": False, "availability": "unknown", "status_code": status, "final_url": final_url, "reason": f"HTTP {status} (probabile anti-bot)"}

    if status >= 400:
        return {"ok": False, "availability": "unknown", "status_code": status, "final_url": final_url, "reason": f"HTTP {status}"}

    negatives = ["sold out", "esaurito", "non disponibile", "tickets not available", "no tickets available"]
    positives = ["acquista", "buy tickets", "aggiungi al carrello", "on sale", "in vendita"]

    if any(k in text for k in negatives):
        return {"ok": True, "availability": "unavailable", "status_code": status, "final_url": final_url, "reason": "negative_keyword"}

    if any(k in text for k in positives):
        return {"ok": True, "availability": "available", "status_code": status, "final_url": final_url, "reason": "positive_keyword"}

    # fallback: se pagina 200 ma non capiamo, lasciamo unknown
    return {"ok": True, "availability": "unknown", "status_code": status, "final_url": final_url, "reason": "no_strong_signals"}


def check_ticketmaster_mapping_availability(*, tm_id: str, url: str) -> Dict[str, Any]:
    """
    Wrapper: normalizza output e aggiunge flags utili al job.
    """
    res = check_ticketmaster_page_availability(url=url)

    # 404 = url invalida: segnalo esplicitamente
    url_invalid = (res.get("status_code") == 404) or ("page_404_invalid_url" in (res.get("reason") or ""))

    return {
        "ok": bool(res.get("ok")),
        "tm_id": tm_id,
        "url": url,
        "final_url": res.get("final_url"),
        "availability": res.get("availability", "unknown"),
        "status_code": res.get("status_code"),
        "url_invalid": url_invalid,
        "reason": res.get("reason"),
    }
