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


def try_with_unlocker(url: str, verbose: bool = False) -> Dict[str, Any]:
    """Usa Bright Data Web Unlocker API per bypassare Akamai su TicketOne."""
    import requests as _req
    import os

    api_key = os.environ.get("BRIGHTDATA_API_KEY", "be81f746-fc93-4632-b4ae-5a86d7f39698")
    zone = os.environ.get("BRIGHTDATA_ZONE", "web_unlocker1")

    if verbose:
        print(f"[UNLOCKER] Requesting via Bright Data Web Unlocker: {url}")

    response = _req.post(
        "https://api.brightdata.com/request",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        json={"zone": zone, "url": url, "format": "raw"},
        timeout=60,
    )

    if response.status_code != 200:
        raise Exception(f"Unlocker HTTP {response.status_code}")

    html = response.text
    if verbose:
        print(f"[UNLOCKER] Got {len(html)} bytes")

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


