import re
from typing import Optional, Tuple


PRICE_VALUE_RE = re.compile(r"(\d+[.,]?\d{0,2})")


def normalize_price_text(price_text: Optional[str]) -> Optional[str]:
    """
    Pulisce il testo prezzo senza cambiarne il significato.
    Esempi:
    - ' da € 49,90 ' -> 'da € 49,90'
    - '49,90  €' -> '49,90 €'
    """
    if not price_text:
        return None

    cleaned = " ".join(str(price_text).split()).strip()
    return cleaned or None


def parse_single_price(price_text: Optional[str]) -> Tuple[Optional[float], Optional[str]]:
    """
    Estrae un singolo prezzo numerico da una stringa TicketOne.

    Esempi supportati:
    - '€ 49,90'
    - '49,90 €'
    - 'da € 39,00'

    Ritorna:
    - valore float
    - currency ('EUR' se trovato un euro o se il formato è coerente con TicketOne)
    """
    price_text = normalize_price_text(price_text)
    if not price_text:
        return None, None

    match = PRICE_VALUE_RE.search(price_text)
    if not match:
        return None, None

    raw_value = match.group(1).replace(",", ".")

    try:
        value = float(raw_value)
    except ValueError:
        return None, None

    currency = "EUR" if "€" in price_text or "eur" in price_text.lower() else "EUR"
    return value, currency