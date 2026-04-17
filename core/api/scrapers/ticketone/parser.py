import re
from typing import List, Optional
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from .schemas import TicketOneEventItem


EVENT_URL_RE = re.compile(r"/event/[^\"'#?]+/?$", re.IGNORECASE)
EXTERNAL_ID_RE = re.compile(r"-(\d{6,})/?$")
DATE_RE = re.compile(r"\b\d{2}/\d{2}/\d{4}\b")
TIME_RE = re.compile(r"\b\d{1,2}:\d{2}\b")
PRICE_RE = re.compile(r"(?:da\s*)?(€\s?\d+[.,]?\d{0,2}|\d+[.,]?\d{0,2}\s?€)", re.IGNORECASE)

KNOWN_CITIES = [
    "Genova",
    "Milano",
    "Roma",
    "Napoli",
    "Torino",
    "Bologna",
    "Firenze",
    "Verona",
    "Padova",
    "Venezia",
    "Bari",
    "Palermo",
    "Catania",
    "Lido di Camaiore",
]


def extract_external_id(url: str) -> Optional[str]:
    match = EXTERNAL_ID_RE.search(url)
    return match.group(1) if match else None


def extract_price_from_text(text: str) -> Optional[str]:
    text = normalize_text(text)
    if not text:
        return None

    match = PRICE_RE.search(text)
    if match:
        return normalize_text(match.group(0))

    return None


def normalize_text(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    cleaned = " ".join(value.split())
    return cleaned.strip() or None


def extract_date_time_from_text(text: str) -> Optional[str]:
    text = normalize_text(text)
    if not text:
        return None

    date_match = DATE_RE.search(text)
    time_match = TIME_RE.search(text)

    if date_match and time_match:
        return f"{date_match.group(0)} {time_match.group(0)}"
    if date_match:
        return date_match.group(0)
    return None


def infer_city_from_text(text: str) -> Optional[str]:
    text = normalize_text(text)
    if not text:
        return None

    if DATE_RE.search(text) or TIME_RE.search(text):
        return None

    low = text.lower()

    blocked_words = [
        "teatro", "arena", "stadio", "politeama", "auditorium",
        "forum", "club", "hall", "festival", "biglietti",
        "ticketone", "ensemble", "orchestra", "day"
    ]
    if any(word in low for word in blocked_words):
        return None

    if len(text.split()) <= 3 and len(text) <= 30:
        return text.title()

    return None


def find_known_city_in_text(text: Optional[str]) -> Optional[str]:
    text = normalize_text(text)
    if not text:
        return None

    low = text.lower()
    for city in KNOWN_CITIES:
        if city.lower() in low:
            return city

    return None


def parse_event_links(html: str, base_url: str, category_hint: Optional[str] = None) -> List[TicketOneEventItem]:
    soup = BeautifulSoup(html, "html.parser")
    items: List[TicketOneEventItem] = []
    seen = set()

    blacklist_exact = {
        "biglietti",
        "ticketone",
        "acquista",
        "compra",
        "info",
        "dettagli",
    }

    def clean_lines_from_container(container) -> List[str]:
        raw_lines = list(container.stripped_strings)
        cleaned = []

        for line in raw_lines:
            line = normalize_text(line)
            if not line:
                continue

            low = line.lower()

            if low in blacklist_exact:
                continue

            if "ticketone" in low and len(line) < 40:
                continue

            cleaned.append(line)

        unique = []
        seen_local = set()
        for line in cleaned:
            key = line.lower()
            if key not in seen_local:
                seen_local.add(key)
                unique.append(line)

        return unique

    def is_bad_title(text: Optional[str]) -> bool:
        if not text:
            return True

        low = text.lower()

        if low in blacklist_exact:
            return True

        if "biglietti" in low:
            return True

        if "ticketone" in low:
            return True

        if DATE_RE.search(text) or TIME_RE.search(text):
            return True

        if len(text) < 8:
            return True

        return False

    def slug_to_title(parsed_path: str) -> Optional[str]:
        slug_part = parsed_path.rstrip("/").split("/")[-1]
        slug_part = EXTERNAL_ID_RE.sub("", slug_part).strip("-/")
        if not slug_part:
            return None
        return normalize_text(slug_part.replace("-", " ").title())

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href:
            continue

        absolute_url = urljoin(base_url, href)
        parsed = urlparse(absolute_url)

        if "ticketone.it" not in parsed.netloc:
            continue

        if not EVENT_URL_RE.search(parsed.path):
            continue

        if absolute_url in seen:
            continue

        seen.add(absolute_url)

        external_id = extract_external_id(parsed.path)

        container = a
        for _ in range(4):
            if container.parent:
                container = container.parent

        container_text = normalize_text(container.get_text(" ", strip=True)) or ""
        price_text = extract_price_from_text(container_text)
        link_text = normalize_text(a.get_text(" ", strip=True)) or ""

        starts_at_raw = extract_date_time_from_text(container_text)
        city = None
        venue = None

        lines = [normalize_text(x) for x in container_text.split("  ")]
        lines = [x for x in lines if x]

        for line in lines:
            if not city:
                city = infer_city_from_text(line)

        if not city:
            city = find_known_city_in_text(container_text)

        for line in lines:
            if line == city:
                continue

            if len(line) < 80 and not DATE_RE.search(line):
                low = line.lower()
                if low not in blacklist_exact and "biglietti" not in low and "ticketone" not in low:
                    if not find_known_city_in_text(line):
                        venue = line
                        break

        title = link_text
        if is_bad_title(title):
            title = slug_to_title(parsed.path)

        if not title:
            title = "Senza titolo"

        items.append(
            TicketOneEventItem(
                title=title,
                event_url=absolute_url,
                external_id=external_id,
                venue=venue,
                city=city,
                starts_at_raw=starts_at_raw,
                category_hint=category_hint,
                source="list",
                detail_status="not_attempted",
                price_text=price_text,
            )
        )

    return items


def pick_better(base: Optional[str], new: Optional[str]) -> Optional[str]:
    if new and (not base or len(new) > len(base)):
        return new
    return base


def parse_event_detail(html: str, item: TicketOneEventItem) -> TicketOneEventItem:
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text("\n", strip=True)

    title = item.title
    h1 = soup.find("h1")
    if h1:
        title = normalize_text(h1.get_text(" ", strip=True)) or title

    city = item.city
    venue = item.venue
    starts_at_raw = item.starts_at_raw
    price_text = item.price_text

    lines = [normalize_text(x) for x in text.splitlines()]
    lines = [x for x in lines if x]

    for line in lines:
        if not starts_at_raw:
            maybe_dt = extract_date_time_from_text(line)
            if maybe_dt:
                starts_at_raw = maybe_dt

        maybe_city = infer_city_from_text(line)
        if maybe_city:
            city = pick_better(city, maybe_city)

        if not city:
            city = find_known_city_in_text(line)

        if line != title and line != city:
            if len(line) < 100:
                if not venue:
                    if not find_known_city_in_text(line):
                        venue = line

        if not price_text:
            maybe_price = extract_price_from_text(line)
            if maybe_price:
                price_text = maybe_price

    return TicketOneEventItem(
        title=title,
        event_url=item.event_url,
        external_id=item.external_id,
        venue=venue,
        city=city,
        starts_at_raw=starts_at_raw,
        category_hint=item.category_hint,
        source="detail",
        detail_status="ok",
        price_text=price_text,
    )