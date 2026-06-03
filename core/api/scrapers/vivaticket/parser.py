# api/scrapers/vivaticket/parser.py

import re
from dataclasses import dataclass
from urllib.parse import urljoin

from bs4 import BeautifulSoup


BASE_URL = "https://www.vivaticket.com"


@dataclass
class VivaticketEvent:
    title: str
    url: str
    external_id: str | None = None
    city: str | None = None
    venue: str | None = None
    raw_date: str | None = None
    price_text: str | None = None
    raw_text: str | None = None


ITALIAN_CITIES = [
    "Roma", "Milano", "Napoli", "Torino", "Palermo", "Genova", "Bologna",
    "Firenze", "Bari", "Catania", "Venezia", "Verona", "Padova", "Trieste",
    "Brescia", "Parma", "Modena", "Reggio Emilia", "Rimini", "Lecce",
    "Salerno", "Caserta", "Bergamo", "Mantova", "Perugia", "Ancona",
    "Pescara", "Cagliari", "Messina", "Siena", "Pisa", "Lucca", "Arezzo",
    "Ferrara", "Vicenza", "Treviso", "Udine", "Assago", "Monza"
]


def clean_text(value: str | None) -> str:
    if not value:
        return ""

    value = re.sub(r"\s+", " ", value)
    return value.strip()


def extract_external_id(url: str) -> str | None:
    """
    Prova a estrarre un ID numerico dall'URL evento.
    Esempio generico:
    /it/ticket/evento/qualcosa/123456
    """

    matches = re.findall(r"(\d{5,})", url)

    if not matches:
        return None

    return matches[-1]


def extract_raw_date(text: str) -> str | None:
    """
    Cerca date in vari formati:
    - 12/07/2026
    - 12-07-2026
    - 12 luglio 2026
    - 12 lug 2026
    """

    patterns = [
        r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b",
        r"\b\d{1,2}\s+(gennaio|febbraio|marzo|aprile|maggio|giugno|luglio|agosto|settembre|ottobre|novembre|dicembre)\s+\d{4}\b",
        r"\b\d{1,2}\s+(gen|feb|mar|apr|mag|giu|lug|ago|set|ott|nov|dic)\s+\d{4}\b",
    ]

    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return clean_text(match.group(0))

    return None

def extract_price_text(text: str) -> str | None:
    """
    Cerca un prezzo nel testo.
    Formati gestiti:
    - € 25,00
    - 25,00 €
    - da € 25,00
    - a partire da € 25,00
    """

    if not text:
        return None

    patterns = [
        r"(?:a partire da|da)?\s*€\s*\d{1,4}(?:[.,]\d{2})?",
        r"(?:a partire da|da)?\s*\d{1,4}(?:[.,]\d{2})?\s*€",
        r"\bEUR\s*\d{1,4}(?:[.,]\d{2})?",
    ]

    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return clean_text(match.group(0))

    return None
def extract_city(text: str) -> str | None:
    """
    Prima versione semplice:
    cerca una città italiana nota dentro il testo della card.
    """

    for city in ITALIAN_CITIES:
        pattern = rf"\b{re.escape(city)}\b"
        if re.search(pattern, text, flags=re.IGNORECASE):
            return city

    return None


def extract_venue(text: str, title: str, city: str | None, raw_date: str | None) -> str | None:
    """
    Prova a stimare la venue togliendo titolo, città e data.
    Non è ancora perfetto, ma aiuta nei casi compatti.
    """

    candidate = text

    if title:
        candidate = candidate.replace(title, " ")

    if city:
        candidate = re.sub(rf"\b{re.escape(city)}\b", " ", candidate, flags=re.IGNORECASE)

    if raw_date:
        candidate = candidate.replace(raw_date, " ")

    candidate = re.sub(r"\bAcquista\b", " ", candidate, flags=re.IGNORECASE)
    candidate = re.sub(r"\bBiglietti\b", " ", candidate, flags=re.IGNORECASE)
    candidate = re.sub(r"\bDisponibile\b", " ", candidate, flags=re.IGNORECASE)
    candidate = re.sub(r"\bNon disponibile\b", " ", candidate, flags=re.IGNORECASE)

    candidate = clean_text(candidate)

    if not candidate:
        return None

    if len(candidate) < 3:
        return None

    return candidate


def is_probable_event_url(href: str) -> bool:
    """
    Filtra link probabili evento.
    La regola è volutamente larga perché Vivaticket può cambiare struttura URL.
    """

    if not href:
        return False

    href = href.lower()

    excluded = [
        "login",
        "account",
        "carrello",
        "cart",
        "privacy",
        "cookie",
        "terms",
        "condizioni",
        "assistenza",
        "help",
        "facebook",
        "instagram",
        "youtube",
    ]

    if any(x in href for x in excluded):
        return False

    if "/it/ticket/" in href:
        return True

    if "/ticket/" in href and re.search(r"\d{5,}", href):
        return True

    return False


def get_card_text(anchor) -> str:
    """
    Risale alcuni livelli dal link per prendere il testo della card evento.
    """

    current = anchor

    best_text = clean_text(anchor.get_text(" ", strip=True))

    for _ in range(6):
        if not current:
            break

        text = clean_text(current.get_text(" ", strip=True))

        if len(text) > len(best_text):
            best_text = text

        current = current.parent

    return best_text


def guess_title(anchor_text: str, card_text: str) -> str:
    """
    Il titolo migliore di solito è nel testo del link.
    Se è vuoto, usiamo l'inizio della card.
    """

    anchor_text = clean_text(anchor_text)

    if anchor_text and len(anchor_text) > 2:
        return anchor_text

    # fallback: togliamo data e prendiamo una parte iniziale
    text = clean_text(card_text)
    raw_date = extract_raw_date(text)

    if raw_date:
        text = text.replace(raw_date, " ")

    text = clean_text(text)

    if len(text) > 120:
        text = text[:120].strip()

    return text


def parse_vivaticket_events(html: str, base_url: str = BASE_URL) -> list[VivaticketEvent]:
    soup = BeautifulSoup(html, "html.parser")

    events: list[VivaticketEvent] = []
    seen: set[str] = set()

    anchors = soup.find_all("a", href=True)

    for anchor in anchors:
        href = anchor.get("href")

        if not is_probable_event_url(href):
            continue

        url = urljoin(base_url, href)

        external_id = extract_external_id(url)

        # Chiave di deduplica
        key = external_id or url

        if key in seen:
            continue

        seen.add(key)

        anchor_text = clean_text(anchor.get_text(" ", strip=True))
        card_text = get_card_text(anchor)

        title = guess_title(anchor_text, card_text)
        raw_date = extract_raw_date(card_text)
        price_text = extract_price_text(card_text)
        city = extract_city(card_text)
        venue = extract_venue(card_text, title, city, raw_date)
        event = VivaticketEvent(
            title=title,
            url=url,
            external_id=external_id,
            city=city,
            venue=venue,
            raw_date=raw_date,
            price_text=price_text,
            raw_text=card_text,
        )

        events.append(event)

    return events