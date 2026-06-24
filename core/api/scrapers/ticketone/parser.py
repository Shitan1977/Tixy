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

def is_bad_location_value(value: Optional[str]) -> bool:
    value = normalize_text(value)
    if not value:
        return True

    low = value.lower()

    bad_parts = [
        "sommario eventi",
        "eventi internazionali",
        "www.ticketone.it",
        "ticketone.it",
        "consenso ai cookie",
        "cookie",
        "privacy",
        "newsletter",
        "login",
        "registrati",
        "carrello",
        "biglietti",
    ]

    return any(part in low for part in bad_parts)


def infer_location_from_ticketone_url(url: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """
    Fallback prudente per URL TicketOne quando il dettaglio restituisce
    città/venue sporche o mancanti.

    La logica usa lo slug dell'URL TicketOne.
    Serve soprattutto per lo scrub automatico.

    NOTA: le entry con city=None sono venue note ma non localizzabili
    con certezza dallo slug. In quel caso la venue viene impostata
    ma la città rimane None → l'evento verrà ancora scartato da
    _has_valid_location. Questo è corretto: non inventiamo città.
    """

    url = normalize_text(url)
    if not url:
        return None, None

    low = url.lower()

    location_map = [
        # ----------------------------------------------------------------
        # Roma
        # ----------------------------------------------------------------
        ("tor-vergata", "Roma", "Tor Vergata"),
        ("stadio-olimpico", "Roma", "Stadio Olimpico"),
        ("ippodromo-le-capannelle", "Roma", "Ippodromo Le Capannelle"),
        ("circo-massimo", "Roma", "Circo Massimo"),
        ("teatro-dellopera-di-roma", "Roma", "Teatro dell'Opera di Roma"),
        ("teatro-sala-umberto", "Roma", "Teatro Sala Umberto"),
        ("palazzo-dello-sport", "Roma", "Palazzo dello Sport"),
        ("foro-italico", "Roma", "Foro Italico"),
        ("palazzo-colonna-galleria-colonna", "Roma", "Galleria Colonna"),
        ("palazzo-velli", "Roma", "Palazzo Velli"),
        ("cinecitta-roma", "Roma", "Cinecittà"),

        # ----------------------------------------------------------------
        # Milano e hinterland
        # ----------------------------------------------------------------
        ("unipol-dome-arena-milano", "Milano", "Unipol Dome"),
        ("unipol-dome", "Milano", "Unipol Dome"),
        ("fiera-milano-live", "Milano", "Fiera Milano Live"),
        ("parco-della-musica-di-milano", "Milano", "Parco della Musica di Milano"),
        ("ippodromo-snai-san-siro", "Milano", "Ippodromo SNAI San Siro"),
        ("stadio-san-siro", "Milano", "Stadio San Siro"),
        ("san-siro", "Milano", "Stadio San Siro"),
        ("legend-club", "Milano", "Legend Club"),
        ("piazza-sempione", "Milano", "Piazza Sempione"),
        ("fabrique", "Milano", "Fabrique"),
        ("nxt-station", "Milano", "NXT Station"),
        ("teatro-clerici", "Milano", "Teatro Clerici"),
        ("teatro-infinity", "Milano", "Teatro Infinity"),
        ("villa-erba", "Cernobbio", "Villa Erba"),

        # ----------------------------------------------------------------
        # Torino / Piemonte
        # ----------------------------------------------------------------
        ("allianz-stadium", "Torino", "Allianz Stadium"),
        ("inalpi-arena", "Torino", "Inalpi Arena"),
        ("piazza-castello", "Torino", "Piazza Castello"),
        ("palazzetto-dello-sport-borgaro", "Borgaro Torinese", "Palazzetto dello Sport"),
        ("castello-di-lagnasco", "Lagnasco", "Castello di Lagnasco"),
        ("piazza-alfieri", "Asti", "Piazza Alfieri"),

        # ----------------------------------------------------------------
        # Veneto / Trentino / Friuli
        # ----------------------------------------------------------------
        ("kioene-arena", "Padova", "Kioene Arena"),
        ("castello-carrarese", "Padova", "Castello Carrarese"),
        ("parcheggio-nord-stadio-euganeo", "Padova", "Stadio Euganeo"),
        ("arena-di-verona", "Verona", "Arena di Verona"),
        ("castello-di-villafranca", "Verona", "Castello di Villafranca"),
        ("teatro-romano-fiesole", "Fiesole", "Teatro Romano di Fiesole"),
        ("villa-manin", "Codroipo", "Villa Manin"),
        ("bluenergy-stadium", "Udine", "Bluenergy Stadium"),
        ("palaunical-arena", "Udine", "PalaUnical Arena"),
        ("doss-del-sabion", "Trento", "Doss del Sabion"),
        ("lago-superiore-di-fusine", "Tarvisio", "Lago Superiore di Fusine"),
        ("forte-di-bard", "Bard", "Forte di Bard"),

        # ----------------------------------------------------------------
        # Emilia-Romagna
        # ----------------------------------------------------------------
        ("teatro-europauditorium", "Bologna", "Teatro EuropAuditorium"),
        ("unipol-arena", "Bologna", "Unipol Arena"),
        ("bolognafiere-arena", "Bologna", "BolognaFiere Arena"),
        ("castello-estense", "Ferrara", "Castello Estense"),
        ("parco-urbano-bassani", "Ferrara", "Parco Urbano Bassani"),
        ("piazza-ariostea", "Ferrara", "Piazza Ariostea"),
        ("autodromo-internazionale-enzo-e-dino-ferrari", "Imola", "Autodromo Enzo e Dino Ferrari"),
        ("parco-san-valentino", "Parma", "Parco San Valentino"),

        # ----------------------------------------------------------------
        # Toscana / Liguria
        # ----------------------------------------------------------------
        ("cava-di-roselle", "Grosseto", "Cava di Roselle"),
        ("bussoladomani", "Lido di Camaiore", "Bussoladomani"),
        ("mura-di-lucca", "Lucca", "Mura di Lucca"),
        ("ippodromo-del-visarno", "Firenze", "Ippodromo del Visarno"),
        ("teatro-cartiere-carrara-extuscanyhall", "Firenze", "ExTuscanyhall"),
        ("area-verde-capannori", "Capannori", "Area Verde Capannori"),
        ("autodromo-internazionale-del-mugello", "Scarperia", "Autodromo del Mugello"),
        ("politeama-genovese", "Genova", "Politeama Genovese"),
        ("arena-mare-area-porto-antico", "Genova", "Arena del Mare Porto Antico"),

        # ----------------------------------------------------------------
        # Lombardia (fuori Milano)
        # ----------------------------------------------------------------
        ("teatro-del-vittoriale", "Gardone Riviera", "Teatro del Vittoriale"),
        ("cremona-circuit", "Cremona", "Cremona Circuit"),
        ("palazzo-martinengo-cesaresco", "Brescia", "Palazzo Martinengo Cesaresco"),
        ("palazzo-te", "Mantova", "Palazzo Te"),
        ("choruslife-arena", "Bergamo", "ChorusLife Arena"),
        ("mao", "Torino", "MAO - Museo d'Arte Orientale"),

        # ----------------------------------------------------------------
        # Umbria / Marche / Abruzzo
        # ----------------------------------------------------------------
        ("anfiteatro-giovanni-paolo-ii", "Assisi", "Anfiteatro Giovanni Paolo II"),
        ("giardini-del-frontone", "Perugia", "Giardini del Frontone"),
        ("arena-santa-giuliana", "Perugia", "Arena Santa Giuliana"),

        # ----------------------------------------------------------------
        # Campania / Basilicata
        # ----------------------------------------------------------------
        ("anfiteatro-scavi-di-pompei", "Pompei", "Anfiteatro degli Scavi di Pompei"),
        ("anfiteatro-degli-scavi", "Pompei", "Anfiteatro degli Scavi di Pompei"),
        ("reggia-di-caserta", "Caserta", "Reggia di Caserta"),
        ("arena-del-mare", "Napoli", "Arena del Mare"),
        ("arena-campo-marte", "Napoli", "Arena Campo Marte"),
        ("piazza-del-plebiscito", "Napoli", "Piazza del Plebiscito"),
        ("palateknoship", "Napoli", "PalaTeknoShip"),

        # ----------------------------------------------------------------
        # Puglia / Calabria
        # ----------------------------------------------------------------
        ("fiera-del-levante", "Bari", "Fiera del Levante"),
        ("grotte-di-castellana", "Castellana Grotte", "Grotte di Castellana"),

        # ----------------------------------------------------------------
        # Sicilia / Sardegna
        # ----------------------------------------------------------------
        ("villa-bellini", "Catania", "Villa Bellini"),
        ("anfiteatro-falcone-e-borsellino", "Zafferana Etnea", "Anfiteatro Falcone e Borsellino"),
        ("teatro-antico-di-taormina", "Taormina", "Teatro Antico di Taormina"),
        ("teatro-greco-di-tindari", "Tindari", "Teatro Greco di Tindari"),
        ("parco-archeologico-neapolis", "Siracusa", "Parco Archeologico Neapolis"),
        ("olbia-arena", "Olbia", "Olbia Arena"),

        # ----------------------------------------------------------------
        # Venue con city=None: slug non rivela la città con certezza.
        # Vengono comunque inserite così il fallback popola almeno venue,
        # ma _has_valid_location le scarterà ancora finché city è None.
        # Utile come base per future espansioni.
        # ----------------------------------------------------------------
        ("parco-della-pace", None, "Parco della Pace"),
        ("teatro-nuovo", None, "Teatro Nuovo"),
        ("stadio-comunale", None, "Stadio Comunale"),
        ("ex-base-nato", None, "Ex Base NATO"),
        ("piazzale-zenith", None, "Piazzale Zenith"),
        ("piazza-grande", None, "Piazza Grande"),
        ("stadio-a-checcarini", None, "Stadio A. Checcarini"),
        ("area-eventi-selvapiana", None, "Area Eventi Selvapiana"),
    ]

    for slug, city, venue in location_map:
        if slug in low:
            return city, venue

    return None, None

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


def _ticketone_has_instock_offer(html: str) -> bool:
    """
    Determina se l'evento ha biglietti realmente acquistabili.

    PRIORITA' 1 (datalayer TicketOne, il piu' affidabile):
      - event_ticket_price vuoto  -> sold out (come Ultimo Tor Vergata)
      - event_series_availability [0] -> sold out
      Questi campi sono accurati anche quando il listino prezzi HTML
      (ticket-type-price) mostra ancora un prezzo "da" residuo.
      Risolve falsi positivi tipo Sting / Pitbull.

    PRIORITA' 2 (JSON-LD offers): se i campi datalayer non ci sono,
      usa availability schema.org. InStock -> True, tutte OutOfStock -> False.

    DEFAULT: se nessun segnale, True (non bloccare).
    """
    import re

    # --- PRIORITA' 1: datalayer ---
    m_price = re.search(r'event_ticket_price"\s*:\s*"?([^",}\]]*)', html)
    m_avail = re.search(r'event_series_availability"\s*:\s*"?\[?\s*([^",}\]]*)', html)

    price_field_present = m_price is not None
    avail_field_present = m_avail is not None

    if price_field_present or avail_field_present:
        price_empty = price_field_present and (m_price.group(1).strip() == "")
        avail_zero = avail_field_present and (m_avail.group(1).strip() in ("0", ""))
        # Se il datalayer dice esplicitamente sold-out -> False
        if price_empty or avail_zero:
            return False
        # Se il datalayer ha un prezzo valorizzato -> InStock
        if price_field_present and m_price.group(1).strip() != "":
            return True

    # --- PRIORITA' 2: JSON-LD ---
    avail_matches = re.findall(r'"availability"\s*:\s*"(https?://schema\.org/\w+)"', html)
    if not avail_matches:
        return True
    return any("InStock" in a for a in avail_matches)


def parse_event_detail(html: str, item: TicketOneEventItem) -> TicketOneEventItem:
    soup = BeautifulSoup(html, "html.parser")
    has_instock = _ticketone_has_instock_offer(html)
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

    # Pulizia valori sporchi presi da menu/header/footer/cookie.
    if is_bad_location_value(city):
        city = None

    if is_bad_location_value(venue):
        venue = None

    # Fallback da URL, utile per pagine come Ultimo Tor Vergata.
    fallback_city, fallback_venue = infer_location_from_ticketone_url(item.event_url)

    if not city and fallback_city:
        city = fallback_city

    if not venue and fallback_venue:
        venue = fallback_venue

    # Se JSON-LD indica che TUTTE le offerte sono OutOfStock,
    # il prezzo trovato nel testo e' solo un riferimento storico/listino.
    if not has_instock:
        price_text = None

    detail_status = "ok" if price_text else "out_of_stock"

    return TicketOneEventItem(
        title=title,
        event_url=item.event_url,
        external_id=item.external_id,
        venue=venue,
        city=city,
        starts_at_raw=starts_at_raw,
        category_hint=item.category_hint,
        source="detail",
        detail_status=detail_status,
        price_text=price_text,
    )