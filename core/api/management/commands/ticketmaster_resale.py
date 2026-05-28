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


TM_EU_BASE     = "https://app.ticketmaster.eu/mfxapi/v2"
TM_DISC_BASE   = "https://app.ticketmaster.com/discovery/v2"  # fallback pubblico

Availability = Literal["available", "limited", "unavailable", "unknown"]


class TicketmasterError(RuntimeError):
    pass


def _get_api_key() -> str:
    api_key = os.getenv("TICKETMASTER_API_KEY")
    if not api_key:
        raise TicketmasterError("Missing env var TICKETMASTER_API_KEY")
    return api_key


def _mask_key(key: str) -> str:
    """Mostra solo i primi 4 e ultimi 2 chars — mai la chiave completa nei log."""
    if not key or len(key) < 8:
        return "***"
    return f"{key[:4]}...{key[-2:]}"


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


# ------------------------------------------------------------------
# EU mfxapi v2: prezzi (endpoint legacy, richiede permessi separati)
# ------------------------------------------------------------------

def _log_auth_error(status_code: int, response_text: str, request_id: Optional[str]) -> str:
    """
    Costruisce un messaggio di errore diagnostico per 401/403
    senza mai includere la chiave API.
    """
    body_excerpt = (response_text or "")[:200].replace("\n", " ").strip()
    rid = f" request_id={request_id}" if request_id else ""
    return (
        f"EU mfxapi: non autorizzato ({status_code}){rid}. "
        f"body_excerpt={body_excerpt!r}. "
        f"La chiave non ha i permessi per mfxapi EU v2 — "
        f"vedi fallback Discovery API."
    )


def fetch_tm_eu_prices(
    *,
    event_id: str,
    apikey: Optional[str] = None,
    domain: str = "it",
    lang: str = "it",
    timeout: int = 25,
    max_retries_429: int = 6,
) -> PriceResult:
    """
    Tenta prima EU mfxapi v2.
    Se risponde 401/403 (chiave non abilitata per EU),
    fa automaticamente fallback sulla Discovery API v2 pubblica
    che restituisce priceRanges e usa la stessa chiave.
    """
    if apikey is None:
        apikey = _get_api_key()

    # ── Tentativo 1: EU mfxapi v2 ────────────────────────────────────────────
    mfx_result = _fetch_mfxapi_prices(
        event_id=event_id,
        apikey=apikey,
        domain=domain,
        lang=lang,
        timeout=timeout,
        max_retries_429=max_retries_429,
    )

    # Se ok o errore non-auth (404, 429 esauriti, eccezione rete), restituisce subito
    if mfx_result.ok:
        return mfx_result
    if mfx_result.status_code not in (401, 403):
        return mfx_result

    # ── Fallback: Discovery API v2 pubblica ──────────────────────────────────
    # mfxapi EU ha risposto 401/403 → la chiave non è abilitata per quell'endpoint.
    # Discovery API v2 usa la stessa chiave ed è accessibile con account standard.
    disc_result = _fetch_discovery_prices(
        event_id=event_id,
        apikey=apikey,
        timeout=timeout,
    )

    # Aggiungiamo nota diagnostica sulla ragione del fallback
    if disc_result.ok:
        disc_result.reason = (
            f"mfxapi EU {mfx_result.status_code} → fallback Discovery API v2: ok"
        )
    else:
        disc_result.reason = (
            f"mfxapi EU {mfx_result.status_code} → fallback Discovery API v2: "
            f"{disc_result.reason}"
        )

    return disc_result


def _fetch_mfxapi_prices(
    *,
    event_id: str,
    apikey: str,
    domain: str,
    lang: str,
    timeout: int,
    max_retries_429: int,
) -> PriceResult:
    url = f"{TM_EU_BASE}/events/{event_id}/prices"
    params = {"apikey": apikey, "domain": domain, "lang": lang}
    headers = {"Accept": "application/json"}

    last_status: Optional[int] = None

    for attempt in range(max_retries_429 + 1):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=timeout)
        except Exception as ex:
            return PriceResult(
                ok=False, status_code=None, availability="unknown",
                min_price=None, max_price=None, currency=None,
                reason=f"mfxapi exception: {ex}", raw=None,
            )

        last_status = r.status_code
        body = (r.text or "")[:600]

        if r.status_code == 429 and attempt < max_retries_429:
            retry_after = r.headers.get("Retry-After")
            try:
                sleep_s = float(retry_after) if retry_after else (2 ** attempt)
            except Exception:
                sleep_s = 2 ** attempt
            time.sleep(sleep_s)
            continue

        if r.status_code == 404:
            return PriceResult(
                ok=False, status_code=404, availability="unknown",
                min_price=None, max_price=None, currency=None,
                reason="EU mfxapi: event_id non trovato (probabile ID non compatibile)",
                raw=None,
            )

        if r.status_code in (401, 403):
            request_id = r.headers.get("X-Request-Id") or r.headers.get("X-TM-Request-Id")
            reason = _log_auth_error(r.status_code, body, request_id)
            return PriceResult(
                ok=False, status_code=r.status_code, availability="unknown",
                min_price=None, max_price=None, currency=None,
                reason=reason, raw=None,
            )

        if r.status_code >= 400:
            return PriceResult(
                ok=False, status_code=r.status_code, availability="unknown",
                min_price=None, max_price=None, currency=None,
                reason=f"EU mfxapi errore HTTP {r.status_code}: {body}",
                raw=None,
            )

        try:
            data = r.json()
        except Exception as ex:
            return PriceResult(
                ok=False, status_code=r.status_code, availability="unknown",
                min_price=None, max_price=None, currency=None,
                reason=f"EU mfxapi JSON parse error: {ex}", raw=None,
            )

        min_price, max_price, currency = _extract_min_max_currency_from_prices_payload(data)
        availability = _guess_availability_from_prices_payload(data, min_price, max_price)
        return PriceResult(
            ok=True, status_code=r.status_code, availability=availability,
            min_price=min_price, max_price=max_price, currency=currency,
            reason=None, raw=data if isinstance(data, dict) else None,
        )

    return PriceResult(
        ok=False, status_code=last_status, availability="unknown",
        min_price=None, max_price=None, currency=None,
        reason="Troppi 429 (rate limit) su EU mfxapi", raw=None,
    )


def _fetch_discovery_prices(
    *,
    event_id: str,
    apikey: str,
    timeout: int,
) -> PriceResult:
    """
    Fallback: Discovery API v2 pubblica.
    Endpoint: GET /discovery/v2/events/{id}.json
    Restituisce priceRanges nel payload evento.
    Usa la stessa apikey come query param — nessun permesso aggiuntivo richiesto.
    """
    url = f"{TM_DISC_BASE}/events/{event_id}.json"
    params = {"apikey": apikey}
    headers = {"Accept": "application/json"}

    MAX_DISC_RETRIES = 2  # solo per 429 — non esageriamo

    last_status: Optional[int] = None

    for attempt in range(MAX_DISC_RETRIES + 1):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=timeout)
        except Exception as ex:
            return PriceResult(
                ok=False, status_code=None, availability="unknown",
                min_price=None, max_price=None, currency=None,
                reason=f"Discovery API exception: {ex}", raw=None,
            )

        last_status = r.status_code
        body = (r.text or "")[:600]

        # 429: rispetta Retry-After se presente, poi riprova (max 2 volte)
        if r.status_code == 429:
            if attempt < MAX_DISC_RETRIES:
                retry_after = r.headers.get("Retry-After")
                try:
                    sleep_s = float(retry_after) if retry_after else (2 ** (attempt + 1))
                except Exception:
                    sleep_s = 2 ** (attempt + 1)
                # Cap a 30s per non bloccare il run troppo a lungo
                sleep_s = min(sleep_s, 30.0)
                time.sleep(sleep_s)
                continue
            # Tentativi esauriti — restituisce 429 così _fetch_prices_with_state
            # può impostare halted=True
            return PriceResult(
                ok=False, status_code=429, availability="unknown",
                min_price=None, max_price=None, currency=None,
                reason=f"Discovery API: rate limit (429) dopo {MAX_DISC_RETRIES + 1} tentativi",
                raw=None,
            )

        if r.status_code == 404:
            return PriceResult(
                ok=False, status_code=404, availability="unknown",
                min_price=None, max_price=None, currency=None,
                reason="Discovery API: event_id non trovato",
                raw=None,
            )

        if r.status_code in (401, 403):
            request_id = r.headers.get("X-Request-Id") or r.headers.get("X-TM-Request-Id")
            rid = f" request_id={request_id}" if request_id else ""
            return PriceResult(
                ok=False, status_code=r.status_code, availability="unknown",
                min_price=None, max_price=None, currency=None,
                reason=f"Discovery API: non autorizzato ({r.status_code}){rid}. body={body[:200]!r}",
                raw=None,
            )

        if r.status_code >= 400:
            return PriceResult(
                ok=False, status_code=r.status_code, availability="unknown",
                min_price=None, max_price=None, currency=None,
                reason=f"Discovery API errore HTTP {r.status_code}: {body}",
                raw=None,
            )

        break  # 2xx — usciamo dal loop

    try:
        data = r.json()
    except Exception as ex:
        return PriceResult(
            ok=False, status_code=r.status_code, availability="unknown",
            min_price=None, max_price=None, currency=None,
            reason=f"Discovery API JSON parse error: {ex}", raw=None,
        )

    # Discovery API restituisce priceRanges direttamente nell'oggetto evento
    min_price, max_price, currency = _extract_min_max_currency_from_prices_payload(data)
    availability = _guess_availability_from_prices_payload(data, min_price, max_price)

    return PriceResult(
        ok=True, status_code=r.status_code, availability=availability,
        min_price=min_price, max_price=max_price, currency=currency,
        reason=None,
        raw=data if isinstance(data, dict) else None,
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


# ------------------------------------------------------------------
# HTML: availability + resale
# ------------------------------------------------------------------

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

_WEAK_POSITIVES = [
    "acquista",
    "buy tickets",
    "aggiungi al carrello",
    "on sale",
    "in vendita",
]

_RESALE_KEYWORDS = [
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
    if any(k in text_lower for k in _RESALE_KEYWORDS):
        return True
    if re.search(r'"isresale"\s*:\s*true', text_lower, flags=re.IGNORECASE):
        return True
    if re.search(r"\bisresale\b\s*:\s*true", text_lower, flags=re.IGNORECASE):
        return True
    return False


def _detect_price_like_text(text_lower: str) -> bool:
    """
    Rileva un prezzo nel DOM/testo.

    Pattern aggiuntivi rispetto alla versione originale:
    - prezzi JSON inline senza simbolo € (es. "price":150.00 nei tag <script>)
    - formato ticketmaster IT: "facevalue":{"amount":150.0}
    """
    price_patterns = [
        # Simbolo € esplicito
        r"€\s?\d+",
        r"\d+,\d{2}\s?€",
        r"\d+\.\d{2}\s?€",
        r"eur\s?\d+",
        r"\d+,\d{2}\s?eur",
        r"\d+\.\d{2}\s?eur",
        # JSON inline nei <script> — Ticketmaster IT usa spesso questi
        r'"price"\s*:\s*\d+(?:\.\d+)?',
        r'"facevalue"\s*:\s*\{[^}]*"amount"\s*:\s*\d+',
        r'"amount"\s*:\s*\d+(?:\.\d+)?',
        r'"minprice"\s*:\s*\d+(?:\.\d+)?',
        r'"maxprice"\s*:\s*\d+(?:\.\d+)?',
        r'"listprice"\s*:\s*\d+(?:\.\d+)?',
    ]
    return any(re.search(p, text_lower, flags=re.IGNORECASE) for p in price_patterns)


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
                    ok=True, availability="unknown", is_resale=False,
                    status_code=404, final_url=last_final_url,
                    reason="page_404_invalid_url", sample=None,
                )

            if r.status_code in (403, 429) or (500 <= r.status_code <= 599):
                if attempt < max_retries:
                    sleep_s = (2 ** attempt) + random.uniform(0.2, 0.8)
                    time.sleep(sleep_s)
                    continue
                return HtmlResult(
                    ok=False, availability="unknown", is_resale=False,
                    status_code=r.status_code, final_url=last_final_url,
                    reason=f"HTTP {r.status_code} (blocked/rate/5xx)", sample=None,
                )

            if r.status_code >= 400:
                return HtmlResult(
                    ok=False, availability="unknown", is_resale=False,
                    status_code=r.status_code, final_url=last_final_url,
                    reason=f"HTTP {r.status_code}", sample=None,
                )

            text_lower = (r.text or "").lower()
            is_resale = _detect_resale(text_lower)
            has_price = _detect_price_like_text(text_lower)
            has_weak_positive = any(k in text_lower for k in _WEAK_POSITIVES)

            if any(k in text_lower for k in _NEGATIVES):
                return HtmlResult(
                    ok=True, availability="unavailable", is_resale=is_resale,
                    status_code=r.status_code, final_url=last_final_url,
                    reason="negative_keyword", sample=text_lower[:220],
                )

            if is_resale and has_price:
                return HtmlResult(
                    ok=True, availability="available", is_resale=True,
                    status_code=r.status_code, final_url=last_final_url,
                    reason="resale_price_strong_signal", sample=text_lower[:220],
                )

            if is_resale:
                return HtmlResult(
                    ok=True, availability="unknown", is_resale=True,
                    status_code=r.status_code, final_url=last_final_url,
                    reason="resale_keyword_without_price", sample=text_lower[:220],
                )

            if has_weak_positive:
                return HtmlResult(
                    ok=True, availability="unknown", is_resale=False,
                    status_code=r.status_code, final_url=last_final_url,
                    reason="weak_positive_ignored_for_resale", sample=text_lower[:220],
                )

            return HtmlResult(
                ok=True, availability="unknown", is_resale=False,
                status_code=r.status_code, final_url=last_final_url,
                reason="no_strong_resale_signals", sample=text_lower[:220],
            )

        except Exception as ex:
            if attempt < max_retries:
                sleep_s = (2 ** attempt) + random.uniform(0.2, 0.8)
                time.sleep(sleep_s)
                continue
            return HtmlResult(
                ok=False, availability="unknown", is_resale=False,
                status_code=last_status, final_url=last_final_url,
                reason=f"exception: {ex}", sample=None,
            )

    return HtmlResult(
        ok=False, availability="unknown", is_resale=False,
        status_code=last_status, final_url=last_final_url,
        reason="unexpected_fallthrough", sample=None,
    )


# ------------------------------------------------------------------
# Merge signals
# ------------------------------------------------------------------

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

    if html.ok and html.availability == "unavailable":
        # HTML dice esplicitamente non disponibile — vince su tutto
        out.availability = "unavailable"
        out.source = "html"

    elif html.ok and html.availability == "available" and html.is_resale:
        # HTML ha trovato resale + prezzo nel DOM: segnale forte autonomo
        out.availability = "available"
        out.source = "html"

    elif (
        html.ok
        and html.is_resale
        and html.availability == "unknown"
        and prices.ok
        and prices.availability in ("available", "limited")
        and (prices.min_price is not None or prices.max_price is not None)
    ):
        # HTML conferma resale ma senza prezzo visibile nel DOM (pagina SPA/JS);
        # prices (mfxapi EU o Discovery API fallback) confermano disponibilità
        # con almeno un prezzo valorizzato.
        # Solo questa combinazione è sufficiente per un alert — né html da solo
        # (resale senza prezzo) né prices da solo (potrebbero essere biglietti
        # standard, non rivendita) basterebbero.
        out.availability = "available"
        out.source = "html+prices"
        out.reason = f"resale_confirmed_by_prices: html={html.reason} prices={prices.reason}"

    elif html.ok:
        # Tutti gli altri casi html.ok: copia availability da html as-is
        # (può essere "unknown" — non genera alert)
        out.availability = html.availability
        out.source = "html"

    else:
        out.availability = "unknown"
        out.source = "mixed"

    return out


# ------------------------------------------------------------------
# Django command entrypoint
# ------------------------------------------------------------------

class Command(BaseCommand):
    help = "Ticketmaster resale probe: HTML availability + EU mfxapi prices (+ Discovery API fallback) + resale detection"

    def add_arguments(self, parser):
        parser.add_argument("--url", required=True)
        parser.add_argument("--event-id", required=False)
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
            url=url, timeout=timeout, session=sess, max_retries=max_retries,
        )

        if event_id:
            price_res = fetch_tm_eu_prices(
                event_id=event_id, domain=domain, lang=lang,
            )
        else:
            price_res = PriceResult(
                ok=False, status_code=None, availability="unknown",
                min_price=None, max_price=None, currency=None,
                reason="event_id not provided: skipped mfxapi", raw=None,
            )

        combined = merge_tm_signals(html_res, price_res)
        payload = asdict(combined)
        self.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2 if pretty else None))