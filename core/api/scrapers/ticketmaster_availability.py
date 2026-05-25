from __future__ import annotations

import os
import random
import time
from typing import Any, Dict, Literal, Optional

import requests


TM_DISCOVERY_BASE = "https://app.ticketmaster.com/discovery/v2"

Availability = Literal["available", "unavailable", "unknown"]


class TicketmasterError(RuntimeError):
    pass


def _get_api_key() -> str:
    api_key = os.getenv("TICKETMASTER_API_KEY")

    if not api_key:
        raise TicketmasterError("Missing env var TICKETMASTER_API_KEY")

    return api_key


def fetch_tm_discovery_event(
    *,
    event_id: str,
    apikey: Optional[str] = None,
    timeout: int = 20,
) -> Dict[str, Any]:
    """
    Legge un evento dalla Ticketmaster Discovery API ufficiale.

    Endpoint:
        https://app.ticketmaster.com/discovery/v2/events/{event_id}.json

    NOTA: la Discovery API NON è un inventario reale dei biglietti.
    Il risultato viene usato come segnale ausiliario, mai come verdetto finale.
    """
    if apikey is None:
        apikey = _get_api_key()

    url = f"{TM_DISCOVERY_BASE}/events/{event_id}.json"

    params = {
        "apikey": apikey,
        "locale": "*",
    }

    try:
        response = requests.get(url, params=params, timeout=timeout)
    except Exception as ex:
        return {
            "ok": False,
            "status_code": None,
            "availability": "unknown",
            "reason": f"discovery_exception:{ex}",
            "raw": None,
            "api_url": None,
            "api_name": None,
        }

    if response.status_code == 404:
        return {
            "ok": False,
            "status_code": 404,
            "availability": "unknown",
            "reason": "discovery_404_not_found",
            "raw": None,
            "api_url": None,
            "api_name": None,
        }

    if response.status_code == 429:
        return {
            "ok": False,
            "status_code": 429,
            "availability": "unknown",
            "reason": "discovery_429_rate_limit",
            "raw": None,
            "api_url": None,
            "api_name": None,
        }

    if response.status_code in (401, 403):
        return {
            "ok": False,
            "status_code": response.status_code,
            "availability": "unknown",
            "reason": f"discovery_auth_error:{response.status_code}",
            "raw": None,
            "api_url": None,
            "api_name": None,
        }

    if response.status_code >= 400:
        return {
            "ok": False,
            "status_code": response.status_code,
            "availability": "unknown",
            "reason": f"discovery_http_error:{response.status_code}",
            "raw": None,
            "api_url": None,
            "api_name": None,
        }

    try:
        data = response.json()
    except Exception as ex:
        return {
            "ok": False,
            "status_code": response.status_code,
            "availability": "unknown",
            "reason": f"discovery_json_error:{ex}",
            "raw": None,
            "api_url": None,
            "api_name": None,
        }

    availability = _guess_availability_from_discovery_payload(data)

    return {
        "ok": True,
        "status_code": response.status_code,
        "availability": availability,
        "reason": f"discovery_{availability}",
        "raw": data,
        "api_url": data.get("url"),
        "api_name": data.get("name"),
    }


def _guess_availability_from_discovery_payload(data: Any) -> Availability:
    """
    Deduce una disponibilità prudente dalla Discovery API.

    Regole:
    - status cancelled/canceled/offsale => unavailable
    - status onsale + priceRanges       => available  (solo come segnale, mai exit anticipato)
    - status onsale senza priceRanges   => unknown
    - altri stati                       => unknown
    """
    if not isinstance(data, dict):
        return "unknown"

    dates = data.get("dates") or {}
    status = dates.get("status") or {}
    status_code = str(status.get("code") or "").strip().lower()

    if status_code in ("cancelled", "canceled", "offsale"):
        return "unavailable"

    if status_code in ("postponed", "rescheduled"):
        return "unknown"

    price_ranges = data.get("priceRanges")

    if status_code == "onsale" and isinstance(price_ranges, list) and len(price_ranges) > 0:
        return "available"

    return "unknown"


def _extract_ticketmaster_event_code(url: Optional[str]) -> Optional[str]:
    if not url:
        return None

    clean_url = str(url).strip().rstrip("/")

    if not clean_url:
        return None

    if "/event/" in clean_url:
        return clean_url.split("/event/")[-1].split("?")[0].split("#")[0].strip()

    return clean_url.split("/")[-1].split("?")[0].split("#")[0].strip()


def _ticketmaster_urls_match(db_url: Optional[str], api_url: Optional[str]) -> bool:
    db_code = _extract_ticketmaster_event_code(db_url)
    api_code = _extract_ticketmaster_event_code(api_url)

    if not db_code or not api_code:
        return False

    return db_code.lower() == api_code.lower()


def _find_first_keyword(text: str, keywords: list[str]) -> Optional[str]:
    """
    Cerca la prima keyword presente nel testo.
    Restituisce la keyword trovata per tracciabilità nei log.
    """
    for keyword in keywords:
        if keyword in text:
            return keyword

    return None


def check_ticketmaster_page_availability(
    *,
    url: str,
    timeout: int = 20,
    session: Optional[requests.Session] = None,
    max_retries: int = 3,
) -> Dict[str, Any]:
    """
    Controlla la disponibilità leggendo la pagina HTML Ticketmaster.

    REGOLA FONDAMENTALE — i segnali negativi vincono sempre.

    strong_positive_keywords contiene SOLO frasi che implicano un'azione
    di acquisto attiva e inequivocabile. Frasi generiche come "buy tickets"
    o "acquista biglietti" sono state spostate nei weak_positive perché
    Ticketmaster le inserisce nel DOM (JSON-LD, SEO, footer) anche quando
    l'evento non è acquistabile.

    Se non ci sono segnali forti, ritorna unknown.
    Meglio perdere un alert vero che mandare un falso positivo.
    """
    current_session = session or requests.Session()

    ua_pool = [
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_6) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
    ]

    def build_headers(attempt: int) -> Dict[str, str]:
        ua = ua_pool[attempt % len(ua_pool)]

        return {
            "User-Agent": ua,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7",
            "Referer": "https://www.ticketmaster.it/",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
        }

    negative_keywords = [
        # IT — frasi dirette
        "sold out",
        "esaurito",
        "biglietti esauriti",
        "non disponibile",
        "non è disponibile",
        "non è più disponibile",
        "biglietti non disponibili",
        "biglietto non disponibile",
        "attualmente non disponibile",
        "al momento non disponibile",
        "momentaneamente non disponibile",
        "temporaneamente non disponibile",
        "purtroppo non disponibile",
        "prevendita terminata",
        "vendita terminata",
        "evento non disponibile",
        "questa performance non",
        # EN — frasi dirette
        "tickets not available",
        "no tickets available",
        "not available",
        "currently not available",
        "temporarily unavailable",
        "no longer available",
    ]

    # PATCH 1 — strong_positive_keywords ridotte a sole frasi inequivocabili.
    # Frasi rimosse rispetto alla versione precedente:
    #   "acquista biglietti", "buy tickets", "get tickets", "find tickets",
    #   "select tickets", "tickets available"
    # Motivo: Ticketmaster le inserisce nel DOM (JSON-LD/SEO/footer/link correlati)
    # anche quando l'evento non è acquistabile, generando falsi positivi.
    strong_positive_keywords = [
        "aggiungi al carrello",
        "procedi all'acquisto",
        "procedi con l'acquisto",
        "seleziona biglietti",
        "scegli i biglietti",
        "checkout",
    ]

    # PATCH 1 (cont.) — Le frasi rimosse dai strong scendono qui.
    # Un weak_positive da solo non genera "available", solo "unknown".
    weak_positive_keywords = [
        "acquista biglietti",
        "buy tickets",
        "get tickets",
        "find tickets",
        "select tickets",
        "tickets available",
        "disponibile",
        "available",
        "acquista",
        "in vendita",
        "on sale",
        "rivendita",
        "isresale",
    ]

    last_exception: Optional[str] = None
    last_status_code: Optional[int] = None
    last_final_url: Optional[str] = None

    for attempt in range(max_retries + 1):
        try:
            response = current_session.get(
                url,
                headers=build_headers(attempt),
                timeout=timeout,
                allow_redirects=True,
            )

            last_status_code = response.status_code
            last_final_url = response.url

            if response.status_code == 404:
                return {
                    "ok": True,
                    "availability": "unknown",
                    "status_code": 404,
                    "final_url": last_final_url,
                    "reason": "page_404_invalid_url",
                }

            if response.status_code in (403, 429) or 500 <= response.status_code <= 599:
                if attempt < max_retries:
                    sleep_s = (2**attempt) + random.uniform(0.2, 0.8)
                    time.sleep(sleep_s)
                    continue

                return {
                    "ok": False,
                    "availability": "unknown",
                    "status_code": response.status_code,
                    "final_url": last_final_url,
                    "reason": f"HTTP {response.status_code} (blocked/rate/5xx)",
                }

            if response.status_code >= 400:
                return {
                    "ok": False,
                    "availability": "unknown",
                    "status_code": response.status_code,
                    "final_url": last_final_url,
                    "reason": f"HTTP {response.status_code}",
                }

            text = (response.text or "").lower()

            found_negative = _find_first_keyword(text, negative_keywords)

            if found_negative:
                return {
                    "ok": True,
                    "availability": "unavailable",
                    "status_code": response.status_code,
                    "final_url": last_final_url,
                    "reason": f"negative_keyword:{found_negative}",
                }

            found_strong_positive = _find_first_keyword(text, strong_positive_keywords)

            if found_strong_positive:
                return {
                    "ok": True,
                    "availability": "available",
                    "status_code": response.status_code,
                    "final_url": last_final_url,
                    "reason": f"strong_positive_keyword:{found_strong_positive}",
                }

            found_weak_positive = _find_first_keyword(text, weak_positive_keywords)

            if found_weak_positive:
                return {
                    "ok": True,
                    "availability": "unknown",
                    "status_code": response.status_code,
                    "final_url": last_final_url,
                    "reason": f"weak_positive_keyword_ignored:{found_weak_positive}",
                }

            return {
                "ok": True,
                "availability": "unknown",
                "status_code": response.status_code,
                "final_url": last_final_url,
                "reason": "no_strong_signals",
            }

        except Exception as ex:
            last_exception = str(ex)

            if attempt < max_retries:
                sleep_s = (2**attempt) + random.uniform(0.2, 0.8)
                time.sleep(sleep_s)
                continue

            return {
                "ok": False,
                "availability": "unknown",
                "status_code": last_status_code,
                "final_url": last_final_url,
                "reason": f"exception:{last_exception}",
            }

    return {
        "ok": False,
        "availability": "unknown",
        "status_code": last_status_code,
        "final_url": last_final_url,
        "reason": "unexpected_fallthrough",
    }


def check_ticketmaster_browser_availability(
    *,
    url: str,
    timeout: int = 60000,
    wait_ms: int = 5000,
) -> Dict[str, Any]:
    """
    Controlla Ticketmaster con browser reale Playwright.

    Serve perché Ticketmaster carica prezzi/posti in modo dinamico.

    PATCH 2 — positive_context_keywords rafforzate.
    La keyword "biglietti" è stata rimossa: appare in ogni pagina TM
    (anche sold out) e combinata con un prezzo generico (es. eventi correlati)
    generava falsi positivi. Ora il contesto richiede frasi più specifiche
    al flusso di acquisto reale.

    Regola:
    - segnali negativi forti  => unavailable
    - prezzo + contesto forte => available
    - prezzo senza contesto   => unknown
    - nessun segnale          => unknown
    """
    try:
        from playwright.sync_api import sync_playwright
    except Exception as ex:
        return {
            "ok": False,
            "availability": "unknown",
            "status_code": None,
            "final_url": url,
            "reason": f"browser_playwright_import_error:{ex}",
            "price": None,
        }

    import re

    negative_keywords = [
        "non disponibile",
        "biglietti non disponibili",
        "biglietti esauriti",
        "attualmente non disponibile",
        "al momento non disponibile",
        "momentaneamente non disponibile",
        "temporaneamente non disponibile",
        "prevendita terminata",
        "vendita terminata",
        "sold out",
        "esaurito",
        "tickets not available",
        "no tickets available",
        "not available",
    ]

    # PATCH 2 — "biglietti" rimosso: troppo generico, presente in ogni pagina TM.
    # Il contesto deve indicare il flusso di acquisto reale, non solo la parola.
    positive_context_keywords = [
        "posto unico",
        "posti migliori",
        "prezzi più bassi",
        "cad.",
        "+ commissioni",
        "aggiungi al carrello",
        "seleziona biglietti",
        "scegli i biglietti",
        "procedi all'acquisto",
    ]

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)

            page = browser.new_page(
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/122.0.0.0 Safari/537.36"
                ),
                locale="it-IT",
            )

            page.goto(url, wait_until="networkidle", timeout=timeout)
            page.wait_for_timeout(wait_ms)

            final_url = page.url
            text = page.inner_text("body").lower()

            browser.close()

    except Exception as ex:
        return {
            "ok": False,
            "availability": "unknown",
            "status_code": None,
            "final_url": url,
            "reason": f"browser_exception:{ex}",
            "price": None,
        }

    found_negative = _find_first_keyword(text, negative_keywords)

    if found_negative:
        return {
            "ok": True,
            "availability": "unavailable",
            "status_code": 200,
            "final_url": final_url,
            "reason": f"browser_negative_keyword:{found_negative}",
            "price": None,
        }

    price_match = re.search(r"\d{1,4},\d{2}\s*€", text)
    found_context = _find_first_keyword(text, positive_context_keywords)

    if price_match and found_context:
        return {
            "ok": True,
            "availability": "available",
            "status_code": 200,
            "final_url": final_url,
            "reason": f"browser_price_detected:{price_match.group(0)};context:{found_context}",
            "price": price_match.group(0),
        }

    if price_match:
        return {
            "ok": True,
            "availability": "unknown",
            "status_code": 200,
            "final_url": final_url,
            "reason": f"browser_price_without_context:{price_match.group(0)}",
            "price": price_match.group(0),
        }

    return {
        "ok": True,
        "availability": "unknown",
        "status_code": 200,
        "final_url": final_url,
        "reason": "browser_no_price_or_strong_signals",
        "price": None,
    }


def check_ticketmaster_mapping_availability(
    *,
    tm_id: str,
    url: str,
) -> Dict[str, Any]:
    """
    Wrapper usato dagli scanner Ticketmaster.

    Strategia definitiva (con patch):

    1. Discovery API — usata come segnale ausiliario.
       - "unavailable" da Discovery con URL match => ritorna subito unavailable.
       - "available" da Discovery NON esce più anticipatamente: continua
         con HTML + browser per conferma reale.
         PATCH 3: rimosso l'early return su discovery_available per evitare
         falsi positivi (la Discovery API non riflette la disponibilità reale).

    2. HTML statico prudente — segnali negativi forti chiudono subito.

    3. Browser Playwright — fonte più affidabile per prezzi dinamici.
    """

    discovery_result: Dict[str, Any] = {
        "ok": False,
        "availability": "unknown",
        "reason": "discovery_not_called",
        "status_code": None,
        "api_url": None,
        "api_name": None,
    }

    # ------------------------------------------------------------
    # 1. Discovery API ufficiale
    # ------------------------------------------------------------
    try:
        discovery_result = fetch_tm_discovery_event(event_id=tm_id)

        discovery_ok = bool(discovery_result.get("ok"))
        discovery_availability = discovery_result.get("availability")
        discovery_api_url = discovery_result.get("api_url")

        urls_match = _ticketmaster_urls_match(url, discovery_api_url)

        if discovery_ok and not urls_match:
            discovery_result["reason"] = (
                f"discovery_url_mismatch:"
                f"db_code={_extract_ticketmaster_event_code(url)};"
                f"api_code={_extract_ticketmaster_event_code(discovery_api_url)};"
                f"api_name={discovery_result.get('api_name')}"
            )

        elif discovery_ok and urls_match:
            # PATCH 3 — "unavailable" da Discovery con URL match è affidabile:
            # ritorniamo subito senza passare per HTML/browser.
            if discovery_availability == "unavailable":
                return {
                    "ok": True,
                    "tm_id": tm_id,
                    "url": url,
                    "final_url": discovery_api_url or url,
                    "availability": "unavailable",
                    "status_code": discovery_result.get("status_code"),
                    "url_invalid": False,
                    "reason": "discovery_unavailable_url_match",
                    "api_name": discovery_result.get("api_name"),
                    "api_url": discovery_api_url,
                    "price": None,
                }

            # PATCH 3 — "available" da Discovery NON esce più anticipatamente.
            # La Discovery API può dire "onsale" con priceRanges ma non garantisce
            # che esistano posti acquistabili in quel momento.
            # Continuiamo con HTML + browser per avere conferma reale.
            if discovery_availability == "available":
                discovery_result["reason"] = "discovery_available_url_match_needs_confirmation"
            else:
                discovery_result["reason"] = "discovery_unknown_url_match"

    except Exception as ex:
        discovery_result = {
            "ok": False,
            "availability": "unknown",
            "reason": f"discovery_exception:{ex}",
            "status_code": None,
            "api_url": None,
            "api_name": None,
        }

    # ------------------------------------------------------------
    # 2. HTML statico prudente
    # ------------------------------------------------------------
    html_result = check_ticketmaster_page_availability(url=url)

    html_availability = html_result.get("availability", "unknown")
    html_reason = html_result.get("reason") or ""
    html_status_code = html_result.get("status_code")

    url_invalid = html_status_code == 404 or "page_404_invalid_url" in html_reason

    if url_invalid:
        return {
            "ok": bool(html_result.get("ok")),
            "tm_id": tm_id,
            "url": url,
            "final_url": html_result.get("final_url"),
            "availability": "unknown",
            "status_code": html_status_code,
            "url_invalid": True,
            "reason": html_reason,
            "api_name": discovery_result.get("api_name") if isinstance(discovery_result, dict) else None,
            "api_url": discovery_result.get("api_url") if isinstance(discovery_result, dict) else None,
            "price": None,
        }

    if html_availability == "unavailable":
        return {
            "ok": True,
            "tm_id": tm_id,
            "url": url,
            "final_url": html_result.get("final_url"),
            "availability": "unavailable",
            "status_code": html_status_code,
            "url_invalid": False,
            "reason": html_reason,
            "api_name": discovery_result.get("api_name") if isinstance(discovery_result, dict) else None,
            "api_url": discovery_result.get("api_url") if isinstance(discovery_result, dict) else None,
            "price": None,
        }

    if html_availability == "available":
        return {
            "ok": True,
            "tm_id": tm_id,
            "url": url,
            "final_url": html_result.get("final_url"),
            "availability": "available",
            "status_code": html_status_code,
            "url_invalid": False,
            "reason": html_reason,
            "api_name": discovery_result.get("api_name") if isinstance(discovery_result, dict) else None,
            "api_url": discovery_result.get("api_url") if isinstance(discovery_result, dict) else None,
            "price": None,
        }

    # ------------------------------------------------------------
    # 3. Browser Playwright: disponibilità reale renderizzata
    # ------------------------------------------------------------
    browser_result = check_ticketmaster_browser_availability(url=url)

    browser_availability = browser_result.get("availability", "unknown")
    browser_reason = browser_result.get("reason") or ""

    discovery_reason = discovery_result.get("reason") if isinstance(discovery_result, dict) else None

    reason_parts = []

    if html_reason:
        reason_parts.append(f"html:{html_reason}")

    if discovery_reason:
        reason_parts.append(f"discovery:{discovery_reason}")

    if browser_reason:
        reason_parts.append(f"browser:{browser_reason}")

    final_reason = "|".join(reason_parts)

    return {
        "ok": bool(browser_result.get("ok")) or bool(html_result.get("ok")),
        "tm_id": tm_id,
        "url": url,
        "final_url": browser_result.get("final_url") or html_result.get("final_url"),
        "availability": browser_availability,
        "status_code": browser_result.get("status_code") or html_status_code,
        "url_invalid": False,
        "reason": final_reason,
        "api_name": discovery_result.get("api_name") if isinstance(discovery_result, dict) else None,
        "api_url": discovery_result.get("api_url") if isinstance(discovery_result, dict) else None,
        "price": browser_result.get("price"),
    }