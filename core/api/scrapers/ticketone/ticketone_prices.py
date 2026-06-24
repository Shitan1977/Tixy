from typing import Dict, Any

from .client import TicketOneClient
from .browser import TicketOneBrowser
from .parser import parse_event_detail
from .schemas import TicketOneEventItem
from .ticketone_parser import parse_single_price


def build_seed_item(url: str) -> TicketOneEventItem:
    """
    Costruisce un item minimo da usare come contenitore
    per il parsing della pagina dettaglio.
    """
    return TicketOneEventItem(
        title="",
        event_url=url,
        external_id=None,
        venue=None,
        city=None,
        starts_at_raw=None,
        category_hint=None,
        source="price_scan",
        detail_status="not_attempted",
        price_text=None,
    )


def looks_like_generic_ticketone_title(title: str | None) -> bool:
    if not title:
        return True

    low = title.lower().strip()

    generic_titles = {
        "ticketone",
        "ticketone | biglietti",
        "ticketone | biglietti per concerti, spettacolo, sport & cultura",
    }

    return low in generic_titles


def infer_detail_status(title: str | None, raw_price_text: str | None) -> str:
    """
    Restituisce uno stato più realistico del parsing.
    """
    if looks_like_generic_ticketone_title(title):
        return "not_event_page"

    if raw_price_text:
        return "ok"

    return "no_price_found"


def build_result(url: str, detailed_item: TicketOneEventItem, source_used: str) -> Dict[str, Any]:
    min_price, currency = parse_single_price(detailed_item.price_text)
    final_status = infer_detail_status(detailed_item.title, detailed_item.price_text)

    return {
        "event_url": url,
        "title": detailed_item.title,
        "raw_price_text": detailed_item.price_text,
        "min_price": min_price,
        "currency": currency,
        "detail_status": final_status,
        "source_used": source_used,
    }


def try_with_http(url: str, verbose: bool = False) -> Dict[str, Any]:
    client = TicketOneClient(verbose=verbose)
    html = client.get_html(url)
    seed_item = build_seed_item(url)
    detailed_item = parse_event_detail(html, seed_item)
    return build_result(url, detailed_item, source_used="http")


def try_with_browser(url: str, verbose: bool = False, headless: bool = True) -> Dict[str, Any]:
    browser = TicketOneBrowser(headless=headless, verbose=verbose)

    try:
        browser.start()
        html = browser.get_html(url)
        seed_item = build_seed_item(url)
        detailed_item = parse_event_detail(html, seed_item)
        return build_result(url, detailed_item, source_used="browser")
    finally:
        try:
            browser.stop()
        except Exception:
            pass


# Numero di tentativi e soglia minima byte per considerare valida la risposta Unlocker.
# Chiamate ravvicinate su Bright Data possono restituire 0 byte (rate-limit
# temporaneo): in quel caso ritentiamo con backoff invece di arrenderci, dato
# che una singola chiamata pulita funziona regolarmente.
MAX_UNLOCKER_RETRIES = 3
UNLOCKER_MIN_VALID_BYTES = 1000
UNLOCKER_RETRY_BASE_WAIT = 4.0


def try_with_unlocker(url: str, verbose: bool = False) -> Dict[str, Any]:
    """Usa Bright Data Web Unlocker API per bypassare Akamai su TicketOne.

    Ritenta con backoff se la risposta e' vuota o troppo corta (sintomo di
    rate-limit Bright Data su chiamate ravvicinate).
    """
    import requests as _req
    import os
    import time as _time
    api_key = os.environ.get("BRIGHTDATA_API_KEY", "be81f746-fc93-4632-b4ae-5a86d7f39698")
    zone = os.environ.get("BRIGHTDATA_ZONE", "web_unlocker1")

    html = ""
    for attempt in range(1, MAX_UNLOCKER_RETRIES + 1):
        if verbose:
            print(f"[UNLOCKER] Requesting (attempt {attempt}/{MAX_UNLOCKER_RETRIES}): {url}")
        try:
            response = _req.post(
                "https://api.brightdata.com/request",
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                },
                json={"zone": zone, "url": url, "format": "raw"},
                timeout=60,
            )
        except Exception as exc:
            if verbose:
                print(f"[UNLOCKER] request error attempt {attempt}: {exc}")
            if attempt < MAX_UNLOCKER_RETRIES:
                _time.sleep(UNLOCKER_RETRY_BASE_WAIT * attempt)
                continue
            raise

        if response.status_code != 200:
            if response.status_code in (429, 500, 502, 503, 504) and attempt < MAX_UNLOCKER_RETRIES:
                if verbose:
                    print(f"[UNLOCKER] HTTP {response.status_code}, retry dopo backoff")
                _time.sleep(UNLOCKER_RETRY_BASE_WAIT * attempt)
                continue
            raise Exception(f"Unlocker HTTP {response.status_code}")

        html = response.text or ""
        if verbose:
            print(f"[UNLOCKER] Got {len(html)} bytes (attempt {attempt})")

        if len(html) >= UNLOCKER_MIN_VALID_BYTES:
            break

        if attempt < MAX_UNLOCKER_RETRIES:
            if verbose:
                print(f"[UNLOCKER] Risposta corta ({len(html)} byte), retry dopo backoff")
            _time.sleep(UNLOCKER_RETRY_BASE_WAIT * attempt)

    seed_item = build_seed_item(url)
    detailed_item = parse_event_detail(html, seed_item)
    return build_result(url, detailed_item, source_used="unlocker")


def get_ticketone_price_data(
    url: str,
    verbose: bool = False,
    use_browser_fallback: bool = True,
    browser_headless: bool = True,
    use_unlocker: bool = True,
) -> Dict[str, Any]:
    """
    Strategia robusta:
    1. prova HTTP diretto
    2. se fallisce (403 Akamai) -> prova Bright Data Web Unlocker
    3. se fallisce -> prova browser (xvfb)
    """
    try:
        result = try_with_http(url, verbose=verbose)
        if result.get("min_price") is not None:
            return result
        if not use_browser_fallback and not use_unlocker:
            return result
        if verbose:
            print(f"[PRICE FALLBACK] HTTP non sufficiente, provo unlocker url={url}")
    except Exception as exc:
        if verbose:
            print(f"[PRICE HTTP ERROR] url={url} error={exc}")
        if not use_browser_fallback and not use_unlocker:
            return {
                "event_url": url, "title": None, "raw_price_text": None,
                "min_price": None, "currency": None,
                "detail_status": "fetch_error", "source_used": "http", "error": str(exc),
            }

    if use_unlocker:
        try:
            result = try_with_unlocker(url, verbose=verbose)
            if result.get("min_price") is not None or result.get("raw_price_text"):
                return result
            if verbose:
                print(f"[UNLOCKER FALLBACK] Unlocker non ha trovato prezzo url={url}")
        except Exception as exc:
            if verbose:
                print(f"[UNLOCKER ERROR] url={url} error={exc}")

    if not use_browser_fallback:
        return {
            "event_url": url, "title": None, "raw_price_text": None,
            "min_price": None, "currency": None,
            "detail_status": "fetch_error", "source_used": "unlocker",
        }

    try:
        return try_with_browser(url, verbose=verbose, headless=browser_headless)
    except Exception as exc:
        if verbose:
            print(f"[PRICE BROWSER ERROR] url={url} error={exc}")
        return {
            "event_url": url, "title": None, "raw_price_text": None,
            "min_price": None, "currency": None,
            "detail_status": "browser_error", "source_used": "browser", "error": str(exc),
        }


