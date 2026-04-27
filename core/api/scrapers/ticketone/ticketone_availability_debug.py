import re
from typing import List, Dict, Any
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from .client import TicketOneClient
from .parser import EVENT_URL_RE, normalize_text


AVAILABILITY_KEYWORDS = [
    "acquista",
    "biglietti",
    "disponibile",
    "disponibili",
    "ultimi biglietti",
    "sold out",
    "non disponibile",
    "non disponibili",
    "evento cancellato",
    "annullato",
    "coming soon",
]


def extract_availability_hints_from_text(text: str) -> List[str]:
    text_low = (text or "").lower()
    found = []

    for keyword in AVAILABILITY_KEYWORDS:
        if keyword in text_low:
            found.append(keyword)

    return found


def inspect_ticketone_list_page(
    url: str = "https://www.ticketone.it/events/concerti-55/",
    limit: int = 20,
    verbose: bool = False,
) -> List[Dict[str, Any]]:
    """
    Legge la pagina lista TicketOne e prova a capire se nel container evento
    compaiono segnali utili di disponibilità.
    """
    client = TicketOneClient(verbose=verbose)
    html = client.get_html(url)

    soup = BeautifulSoup(html, "html.parser")
    results = []
    seen = set()

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href:
            continue

        absolute_url = urljoin(url, href)
        parsed = urlparse(absolute_url)

        if "ticketone.it" not in parsed.netloc:
            continue

        if not EVENT_URL_RE.search(parsed.path):
            continue

        if absolute_url in seen:
            continue

        seen.add(absolute_url)

        container = a
        for _ in range(4):
            if container.parent:
                container = container.parent

        container_text = normalize_text(container.get_text(" ", strip=True)) or ""
        link_text = normalize_text(a.get_text(" ", strip=True)) or ""
        hints = extract_availability_hints_from_text(container_text)

        results.append({
            "title": link_text,
            "url": absolute_url,
            "hints": hints,
            "container_text": container_text,
        })

        if len(results) >= limit:
            break

    return results