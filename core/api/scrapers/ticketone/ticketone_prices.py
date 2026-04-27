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


def get_ticketone_price_data(
    url: str,
    verbose: bool = False,
    use_browser_fallback: bool = True,
    browser_headless: bool = True,
) -> Dict[str, Any]:
    """
    Strategia robusta:
    - prova HTTP
    - se fallisce o non trova prezzo utile, prova browser
    """
    try:
        result = try_with_http(url, verbose=verbose)

        # Se HTTP ha trovato davvero un prezzo, va bene così
        if result.get("min_price") is not None:
            return result

        # Se HTTP non fallisce ma non produce dato utile,
        # valutiamo il fallback browser
        if not use_browser_fallback:
            return result

        if verbose:
            print(f"[PRICE FALLBACK] HTTP non sufficiente, provo browser url={url}")

    except Exception as exc:
        if verbose:
            print(f"[PRICE HTTP ERROR] url={url} error={exc}")

        if not use_browser_fallback:
            return {
                "event_url": url,
                "title": None,
                "raw_price_text": None,
                "min_price": None,
                "currency": None,
                "detail_status": "fetch_error",
                "source_used": "http",
                "error": str(exc),
            }

    try:
        return try_with_browser(url, verbose=verbose, headless=browser_headless)
    except Exception as exc:
        if verbose:
            print(f"[PRICE BROWSER ERROR] url={url} error={exc}")

        return {
            "event_url": url,
            "title": None,
            "raw_price_text": None,
            "min_price": None,
            "currency": None,
            "detail_status": "browser_error",
            "source_used": "browser",
            "error": str(exc),
        }