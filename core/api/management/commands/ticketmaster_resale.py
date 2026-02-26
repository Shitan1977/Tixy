from __future__ import annotations

import json
import os
import random
import re
import time
from dataclasses import dataclass, asdict
from typing import Any, Dict, Optional, Tuple, Literal

import requests
from django.core.management.base import BaseCommand


TM_EU_BASE = "https://app.ticketmaster.eu/mfxapi/v2"

Availability = Literal["available", "limited", "unavailable", "unknown"]


class TicketmasterError(RuntimeError):
    pass


def _get_api_key() -> str:
    api_key = os.getenv("TICKETMASTER_API_KEY")
    if not api_key:
        raise TicketmasterError("Missing env var TICKETMASTER_API_KEY")
    return api_key


@dataclass
class PriceResult:
    ok: bool
    status_code: Optional[int]
    availability: Availability
    min_price: Optional[float]
    max_price: Optional[float]
    currency: Optional[str]
    reason: Optional[str]
    raw: Optional[dict]


@dataclass
class HtmlResult:
    ok: bool
    availability: Availability
    is_resale: bool
    status_code: Optional[int]
    final_url: Optional[str]
    reason: Optional[str]
    sample: Optional[str] = None  # debug excerpt


@dataclass
class CombinedResult:
    ok: bool
    availability: Availability
    is_resale: bool
    min_price: Optional[float]
    max_price: Optional[float]
    currency: Optional[str]
    source: str
    reason: Optional[str]
    html: Optional[dict]
    prices: Optional[dict]


# ---------------------------
# EU mfxapi: prezzi
# ---------------------------

def fetch_tm_eu_prices(
    *,
    event_id: str,
    apikey: Optional[str] = None,
    domain: str = "it",
    lang: str = "it",
    timeout: int = 25,
    max_retries_429: int = 6,
) -> PriceResult:
    if apikey is None:
        apikey = _get_api_key()

    url = f"{TM_EU_BASE}/events/{event_id}/prices"
    params = {"apikey": apikey, "domain": domain, "lang": lang}
    headers = {"Accept": "application/json"}

    last_status: Optional[int] = None
    last_text: Optional[str] = None

    for attempt in range(max_retries_429 + 1):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=timeout)
        except Exception as ex:
            return PriceResult(
                ok=False,
                status_code=None,
                availability="unknown",
                min_price=None,
                max_price=None,
                currency=None,
                reason=f"mfxapi exception: {ex}",
                raw=None,
            )

        last_status = r.status_code
        last_text = (r.text or "")[:600]

        # rate limit
        if r.status_code == 429 and attempt < max_retries_429:
            retry_after = r.headers.get("Retry-After")
            sleep_s = int(retry_after) if (retry_after and retry_after.isdigit()) else (2 ** attempt)
            time.sleep(sleep_s)
            continue

        if r.status_code == 404:
            return PriceResult(
                ok=False,
                status_code=404,
                availability="unknown",
                min_price=None,
                max_price=None,
                currency=None,
                reason="EU mfxapi: event_id non trovato (probabile ID non compatibile)",
                raw=None,
            )

        if r.status_code in (401, 403):
            return PriceResult(
                ok=False,
                status_code=r.status_code,
                availability="unknown",
                min_price=None,
                max_price=None,
                currency=None,
                reason=f"EU mfxapi: non autorizzato ({r.status_code}). Controllare apikey/permessi.",
                raw=None,
            )

        if r.status_code >= 400:
            return PriceResult(
                ok=False,
                status_code=r.status_code,
                availability="unknown",
                min_price=None,
                max_price=None,
                currency=None,
                reason=f"EU mfxapi errore HTTP {r.status_code}: {last_text}",
                raw=None,
            )

        try:
            data = r.json()
        except Exception as ex:
            return PriceResult(
                ok=False,
                status_code=r.status_code,
                availability="unknown",
                min_price=None,
                max_price=None,
                currency=None,
                reason=f"EU mfxapi JSON parse error: {ex}",
                raw=None,
            )

        min_price, max_price, currency = _extract_min_max_currency_from_prices_payload(data)
        availability = _guess_availability_from_prices_payload(data, min_price, max_price)

        return PriceResult(
            ok=True,
            status_code=r.status_code,
            availability=availability,
            min_price=min_price,
            max_price=max_price,
            currency=currency,
            reason=None,
            raw=data if isinstance(data, dict) else None,
        )

    return PriceResult(
        ok=False,
        status_code=last_status,
        availability="unknown",
        min_price=None,
        max_price=None,
        currency=None,
        reason="Troppi 429 (rate limit) su EU mfxapi",
        raw=None,
    )


def _extract_min_max_currency_from_prices_payload(data: Any) -> Tuple[Optional[float], Optional[float], Optional[str]]:
    if not isinstance(data, dict):
        return None, None, None

    candidates = []

    for key in ("prices", "priceRanges", "price_range", "offers", "levels"):
        arr = data.get(key)
        if isinstance(arr, list):
            for it in arr:
                if isinstance(it, dict):
                    candidates.append(it)

    def walk(obj):
        if isinstance(obj, dict):
            yield obj
            for v in obj.values():
                yield from walk(v)
        elif isinstance(obj, list):
            for v in obj:
                yield from walk(v)

    for obj in walk(data):
        if isinstance(obj, dict) and (
            ("min" in obj and "max" in obj)
            or ("minPrice" in obj and "maxPrice" in obj)
            or ("min_value" in obj and "max_value" in obj)
            or ("value" in obj)
        ):
            candidates.append(obj)

    min_v: Optional[float] = None
    max_v: Optional[float] = None
    curr: Optional[str] = None

    def as_float(x):
        try:
            return float(x)
        except Exception:
            return None

    for it in candidates:
        for ckey in ("currency", "currencyCode", "cur"):
            if it.get(ckey):
                curr = str(it.get(ckey))
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


def _guess_availability_from_prices_payload(data: Any, min_price: Optional[float], max_price: Optional[float]) -> Availability:
    if isinstance(data, dict):
        for key in ("availability", "status", "onSale", "onsale", "available"):
            if key in data:
                s = str(data.get(key)).lower()
                if "sold" in s or "unavail" in s or s in ("false", "0", "no"):
                    return "unavailable"
                if "limit" in s:
                    return "limited"
                if "avail" in s or s in ("true", "1", "yes"):
                    return "available"

    if (min_price is not None) or (max_price is not None):
        return "limited"

    return "unknown"


# ---------------------------
# HTML: availability + resale
# ---------------------------

_UA_POOL = [
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_6) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
]

_NEGATIVES = [
    "sold out",
    "esaurito",
    "non disponibile",
    "tickets not available",
    "no tickets available",
]

_POSITIVES = [
    "acquista",
    "buy tickets",
    "aggiungi al carrello",
    "on sale",
    "in vendita",
    "rivendita",
    "resale",
]


def _build_headers(attempt: int) -> Dict[str, str]:
    ua = _UA_POOL[attempt % len(_UA_POOL)]
    return {
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer": "https://www.ticketmaster.it/",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }


def _detect_resale(text_lower: str) -> bool:
    if ("rivendita" in text_lower) or ("resale" in text_lower):
        return True
    # JSON inline / script tag
    if re.search(r'"isresale"\s*:\s*true', text_lower, flags=re.IGNORECASE):
        return True
    if re.search(r"\bisresale\b\s*:\s*true", text_lower, flags=re.IGNORECASE):
        return True
    return False


def check_ticketmaster_page_availability(
    *,
    url: str,
    timeout: int = 20,
    session: Optional[requests.Session] = None,
    max_retries: int = 4,
) -> HtmlResult:
    s = session or requests.Session()

    last_status: Optional[int] = None
    last_final_url: Optional[str] = None

    for attempt in range(max_retries + 1):
        try:
            r = s.get(
                url,
                headers=_build_headers(attempt),
                timeout=timeout,
                allow_redirects=True,
            )
            last_status = r.status_code
            last_final_url = r.url

            if r.status_code == 404:
                return HtmlResult(
                    ok=True,
                    availability="unknown",
                    is_resale=False,
                    status_code=404,
                    final_url=last_final_url,
                    reason="page_404_invalid_url",
                    sample=None,
                )

            if r.status_code in (403, 429) or (500 <= r.status_code <= 599):
                if attempt < max_retries:
                    sleep_s = (2 ** attempt) + random.uniform(0.2, 0.8)
                    time.sleep(sleep_s)
                    continue
                return HtmlResult(
                    ok=False,
                    availability="unknown",
                    is_resale=False,
                    status_code=r.status_code,
                    final_url=last_final_url,
                    reason=f"HTTP {r.status_code} (blocked/rate/5xx)",
                    sample=None,
                )

            if r.status_code >= 400:
                return HtmlResult(
                    ok=False,
                    availability="unknown",
                    is_resale=False,
                    status_code=r.status_code,
                    final_url=last_final_url,
                    reason=f"HTTP {r.status_code}",
                    sample=None,
                )

            text_lower = (r.text or "").lower()
            is_resale = _detect_resale(text_lower)

            if any(k in text_lower for k in _NEGATIVES):
                return HtmlResult(
                    ok=True,
                    availability="unavailable",
                    is_resale=is_resale,
                    status_code=r.status_code,
                    final_url=last_final_url,
                    reason="negative_keyword",
                    sample=text_lower[:220],
                )

            if any(k in text_lower for k in _POSITIVES):
                return HtmlResult(
                    ok=True,
                    availability="available",
                    is_resale=is_resale,
                    status_code=r.status_code,
                    final_url=last_final_url,
                    reason="positive_keyword",
                    sample=text_lower[:220],
                )

            return HtmlResult(
                ok=True,
                availability="unknown",
                is_resale=is_resale,
                status_code=r.status_code,
                final_url=last_final_url,
                reason="no_strong_signals",
                sample=text_lower[:220],
            )

        except Exception as ex:
            if attempt < max_retries:
                sleep_s = (2 ** attempt) + random.uniform(0.2, 0.8)
                time.sleep(sleep_s)
                continue
            return HtmlResult(
                ok=False,
                availability="unknown",
                is_resale=False,
                status_code=last_status,
                final_url=last_final_url,
                reason=f"exception: {ex}",
                sample=None,
            )

    return HtmlResult(
        ok=False,
        availability="unknown",
        is_resale=False,
        status_code=last_status,
        final_url=last_final_url,
        reason="unexpected_fallthrough",
        sample=None,
    )


# ---------------------------
# Merge signals
# ---------------------------

def merge_tm_signals(html: HtmlResult, prices: PriceResult) -> CombinedResult:
    out = CombinedResult(
        ok=bool(html.ok or prices.ok),
        availability="unknown",
        is_resale=bool(html.is_resale),
        min_price=prices.min_price if prices.ok else None,
        max_price=prices.max_price if prices.ok else None,
        currency=prices.currency if prices.ok else None,
        source="mixed",
        reason=html.reason or prices.reason,
        html=asdict(html),
        prices=asdict(prices),
    )

    if html.ok and html.availability in ("available", "unavailable"):
        out.availability = html.availability
        out.source = "html"
    elif prices.ok and prices.availability != "unknown":
        out.availability = prices.availability
        out.source = "mfxapi"
    elif html.ok:
        out.availability = html.availability
        out.source = "html"
    else:
        out.availability = "unknown"
        out.source = "mixed"

    return out


# ---------------------------
# Django command entrypoint
# ---------------------------

class Command(BaseCommand):
    help = "Ticketmaster resale probe: HTML availability + EU mfxapi prices + resale detection"

    def add_arguments(self, parser):
        parser.add_argument("--url", required=True, help="Ticketmaster event URL (ticketmaster.it/...)")
        parser.add_argument("--event-id", required=False, help="EU mfxapi event_id (optional)")
        parser.add_argument("--domain", default="it")
        parser.add_argument("--lang", default="it")
        parser.add_argument("--timeout", type=int, default=20)
        parser.add_argument("--max-retries", type=int, default=4)
        parser.add_argument("--pretty", action="store_true")

    def handle(self, *args, **opts):
        url = opts["url"]
        event_id = opts.get("event_id")
        domain = opts["domain"]
        lang = opts["lang"]
        timeout = opts["timeout"]
        max_retries = opts["max_retries"]
        pretty = opts["pretty"]

        sess = requests.Session()

        html_res = check_ticketmaster_page_availability(
            url=url,
            timeout=timeout,
            session=sess,
            max_retries=max_retries,
        )

        if event_id:
            price_res = fetch_tm_eu_prices(
                event_id=event_id,
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
                reason="event_id not provided: skipped mfxapi",
                raw=None,
            )

        combined = merge_tm_signals(html_res, price_res)
        payload = asdict(combined)

        self.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2 if pretty else None))