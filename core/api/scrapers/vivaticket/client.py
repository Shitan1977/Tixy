# api/scrapers/vivaticket/client.py

import re
from urllib.parse import urlparse, parse_qs
import time
import requests


API_EVENT_URL = "https://apigatewayb2cstore.vivaticket.com/api/Event/{event_id}/it/it-IT"


def extract_vivaticket_event_id(url: str) -> str | None:
    """
    Estrae l'external_id Vivaticket da una URL evento.

    Esempio:
    https://www.vivaticket.com/it/ticket/sting/287720
    -> 287720
    """

    if not url:
        return None

    matches = re.findall(r"(\d{5,})", url)

    if not matches:
        return None

    return matches[-1]


def fetch_vivaticket_event_api(event_id: str, retries: int = 3) -> dict | None:
    """
    Recupera i dati evento da API Vivaticket.

    Esempio endpoint:
    https://apigatewayb2cstore.vivaticket.com/api/Event/287720/it/it-IT
    """

    if not event_id or not str(event_id).isdigit():
        print(f"[VIVATICKET API] external_id non valido: {event_id!r}")
        return None

    url = API_EVENT_URL.format(event_id=event_id)

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://www.vivaticket.com",
        "Referer": f"https://www.vivaticket.com/it/ticket/event/{event_id}",
        "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
    }

    for attempt in range(retries):
        try:
            response = requests.get(
                url,
                headers=headers,
                timeout=30,
            )

            if response.status_code == 200:
                return response.json()

            if response.status_code == 404:
                print(f"[VIVATICKET API 404] event_id={event_id} url={url}")
                return None

            if response.status_code in [429, 500, 502, 503, 504]:
                wait_seconds = 2 ** attempt
                print(
                    f"[VIVATICKET API RETRY] "
                    f"event_id={event_id} status={response.status_code} "
                    f"attempt={attempt + 1}/{retries} wait={wait_seconds}s"
                )
                time.sleep(wait_seconds)
                continue

            print(f"[VIVATICKET API ERROR] status={response.status_code} url={url}")
            return None

        except requests.RequestException as exc:
            if attempt == retries - 1:
                print(f"[VIVATICKET API REQUEST ERROR] event_id={event_id} error={exc}")
                return None

            wait_seconds = 2 ** attempt
            print(
                f"[VIVATICKET API REQUEST RETRY] "
                f"event_id={event_id} attempt={attempt + 1}/{retries} "
                f"wait={wait_seconds}s error={exc}"
            )
            time.sleep(wait_seconds)

        except ValueError as exc:
            print(f"[VIVATICKET API JSON ERROR] event_id={event_id} error={exc}")
            return None

    return None


def extract_query_param(url: str | None, key: str) -> str | None:
    """
    Estrae un parametro query da una URL.

    Esempio:
    https://shop.vivaticket.com/it/sell/?cmd=prices&pcode=13310085&tcode=vt0020317

    pcode -> 13310085
    tcode -> vt0020317
    """

    if not url:
        return None

    parsed = urlparse(url)
    query = parse_qs(parsed.query)

    values = query.get(key)

    if values and len(values) > 0:
        return values[0]

    return None

def detect_shop_type(shop_url: str | None) -> str:
    """
    Distingue il tipo di shop/acquisto.

    - vivaticket_shop: shop ufficiale con pcode/tcode
    - vivaticket_partner: sottodomini o partner Vivaticket
    - external: shop esterno
    - none: nessun link vendita
    """

    if not shop_url:
        return "none"

    url_lower = shop_url.lower()

    if "shop.vivaticket.com" in url_lower and "cmd=prices" in url_lower:
        return "vivaticket_shop"

    if "vivaticket.com" in url_lower:
        return "vivaticket_partner"

    return "external"
def get_button_by_type(buttons: list[dict], button_type: str) -> dict | None:
    """
    Cerca un bottone dentro la lista buttons usando il campo type.
    """

    for button in buttons:
        if button.get("type") == button_type:
            return button

    return None


def map_vivaticket_sale_status(performance_status, sell_button: dict | None) -> str:
    """
    Mappa provvisoria dello stato vendita Vivaticket.

    Regole attuali:
    - status 100 + bottone Acquista attivo = available
    - status 102 + bottone Acquista attivo = available_or_special
    - bottone non attivo = inactive_sell_button
    - assenza bottone = no_sell_button
    """

    is_sell_active = bool(sell_button and sell_button.get("active"))

    sell_label = ""
    if sell_button:
        sell_label = (sell_button.get("label") or "").lower()

    if "sold" in sell_label or "esaurito" in sell_label or "sold out" in sell_label:
        return "sold_out"

    if "non disponibile" in sell_label or "not available" in sell_label:
        return "not_available"

    if "prossimamente" in sell_label or "soon" in sell_label:
        return "coming_soon"

    if performance_status == 100 and is_sell_active:
        return "available"

    if performance_status == 102 and is_sell_active:
        return "available_or_special"

    if sell_button and not is_sell_active:
        return "inactive_sell_button"

    if not sell_button:
        return "no_sell_button"

    return "unknown"


def normalize_vivaticket_event_api(data: dict, source_url: str | None = None) -> dict | None:
    """
    Normalizza il JSON API Vivaticket in un dizionario semplice.

    Questo metodo legge:
    - infoEvent: dati generali evento, venue, organizzatore
    - infoPerformance: date, performance, status, pulsante acquista, shop_url
    """

    if not data:
        return None

    info_event = data.get("infoEvent") or {}
    info_performance = data.get("infoPerformance") or {}

    event_detail = info_event.get("eventDetail") or {}
    # resaleLink: link diretto alla rivendita. Vuoto = nessuna rivendita reale.
    # Segnale affidabile (a differenza del solo flag resale_active).
    resale_link = (event_detail.get("resaleLink") or "").strip()
    venue_detail = info_event.get("venueDetail") or {}
    location_detail = info_event.get("locationDetail") or {}
    organizer_detail = info_event.get("organizerDetail") or {}

    performances = info_performance.get("performances") or []

    first_performance = performances[0] if performances else {}

    buttons = first_performance.get("buttons") or []

    sell_button = get_button_by_type(buttons, "sell")
    changeuser_button = get_button_by_type(buttons, "changeuser")

    title = (
        event_detail.get("title")
        or first_performance.get("title")
    )

    subtitle = event_detail.get("subTitle")

    raw_date = (
        first_performance.get("label")
        or first_performance.get("datetime")
        or event_detail.get("startDate")
        or event_detail.get("endDate")
    )

    starts_at_raw = (
        first_performance.get("datetime")
        or event_detail.get("startDate")
        or event_detail.get("endDate")
    )

    venue = venue_detail.get("name")
    city = venue_detail.get("city")
    province = venue_detail.get("provinceCode")
    address = venue_detail.get("address")

    currency = (
        event_detail.get("currency")
        or info_event.get("currency")
        or "EUR"
    )

    shop_url = sell_button.get("url") if sell_button else None
    shop_type = detect_shop_type(shop_url)
    changeuser_url = changeuser_button.get("url") if changeuser_button else None

    performance_id = first_performance.get("id")
    performance_code = first_performance.get("code")
    performance_status = first_performance.get("status")

    shop_type = detect_shop_type(shop_url)

    pcode = extract_query_param(shop_url, "pcode")
    tcode = extract_query_param(shop_url, "tcode")

    if not pcode and shop_type == "vivaticket_shop":
        pcode = performance_code

    location_code = (
        tcode
        or location_detail.get("code")
        or location_detail.get("locationCode")
        or location_detail.get("tcode")
    )

    is_sell_active = bool(sell_button and sell_button.get("active"))

    sale_status = map_vivaticket_sale_status(
        performance_status=performance_status,
        sell_button=sell_button,
    )

    dates = info_performance.get("dates") or []
    hours = info_performance.get("hours") or []

    return {
        "title": title,
        "subtitle": subtitle,
        "raw_date": raw_date,
        "starts_at_raw": starts_at_raw,

        "venue": venue,
        "city": city,
        "province": province,
        "address": address,

        "currency": currency,

        "location_code": location_code,
        "venue_id": venue_detail.get("id"),
        "organizer": organizer_detail.get("name"),

        "performance_id": performance_id,
        "performance_code": performance_code,
        "performance_status": performance_status,

        "pcode": pcode,
        "tcode": tcode,

        "is_sell_active": is_sell_active,
        "sale_status": sale_status,
        "resale_link": resale_link,
        "resale_active": bool(resale_link),

        "shop_url": shop_url,
        "shop_type": shop_type,
        "changeuser_url": changeuser_url,

        "dates": dates,
        "hours": hours,

        "source_url": source_url,
    }


def get_vivaticket_event_detail(event_id: str, source_url: str | None = None) -> dict | None:
    """
    Funzione principale.

    Prende un event_id Vivaticket e restituisce dati evento già normalizzati.
    """

    data = fetch_vivaticket_event_api(event_id)

    if not data:
        return None

    return normalize_vivaticket_event_api(
        data=data,
        source_url=source_url,
    )


def get_vivaticket_event_detail_from_url(source_url: str) -> dict | None:
    """
    Funzione comoda: prende direttamente una URL Vivaticket,
    estrae l'event_id e restituisce i dati evento normalizzati.
    """

    event_id = extract_vivaticket_event_id(source_url)

    if not event_id:
        print(f"[VIVATICKET URL ERROR] impossibile estrarre event_id da {source_url}")
        return None

    return get_vivaticket_event_detail(
        event_id=event_id,
        source_url=source_url,
    )