from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin, urlparse
import re
import json
import requests
from bs4 import BeautifulSoup


@dataclass
class TicketSwapEvent:
    external_event_id: str
    title: str
    url: str
    starts_at_iso: Optional[str]
    venue_name: Optional[str]
    city: Optional[str]
    country: Optional[str]
    image_url: Optional[str]
    raw: Dict[str, Any]


class TicketSwapScraper:
    """
    Scraper "public" (no API key) per ottenere eventi TicketSwap Italia.
    Strategia:
      1) Legge la pagina Location Italia (con ID)
      2) Estrae link /event/...
      3) Per ogni event page prova a leggere JSON-LD (schema.org) per titolo/data/venue/immagine
    """

    BASE = "https://www.ticketswap.com"
    # URL location Italia (con id). Esempio reale: /location/italy/10896
    LOCATION_ITALY = "https://www.ticketswap.com/location/italy/10896"

    def __init__(self, timeout: int = 25):
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
        })

    def _get_html(self, url: str, params: Optional[dict] = None) -> str:
        r = self.session.get(url, params=params, timeout=self.timeout, allow_redirects=True)
        r.raise_for_status()
        return r.text

    def _extract_event_links_from_location(self, html: str) -> List[str]:
        soup = BeautifulSoup(html, "html.parser")
        links = set()
        for a in soup.select("a[href]"):
            href = a.get("href") or ""
            if "/event/" in href:
                full = urljoin(self.BASE, href)
                links.add(full.split("?")[0].rstrip("/"))
        return sorted(links)

    def _parse_jsonld_event(self, html: str) -> Optional[dict]:
        soup = BeautifulSoup(html, "html.parser")
        for script in soup.select('script[type="application/ld+json"]'):
            txt = (script.string or "").strip()
            if not txt:
                continue
            try:
                data = json.loads(txt)
            except Exception:
                continue

            # JSON-LD può essere dict o lista
            candidates = []
            if isinstance(data, dict):
                candidates = [data]
            elif isinstance(data, list):
                candidates = data

            for obj in candidates:
                if not isinstance(obj, dict):
                    continue
                t = (obj.get("@type") or obj.get("['@type']") or "")
                if isinstance(t, list):
                    # spesso ["Event", ...]
                    if "Event" in t:
                        return obj
                else:
                    if str(t).lower() == "event":
                        return obj
        return None

    def _event_id_from_url(self, url: str) -> str:
        """
        TicketSwap spesso usa url /event/<slug>/<uuid>
        prendiamo l'uuid se c'è, altrimenti fallback al path.
        """
        path = urlparse(url).path.strip("/")
        # prova uuid
        m = re.search(r"/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$", "/" + path, re.I)
        if m:
            return m.group(1)
        # fallback: path intero
        return path.replace("/", "_")

    def _build_event_from_page(self, url: str, html: str) -> Optional[TicketSwapEvent]:
        jsonld = self._parse_jsonld_event(html)

        # fallback base
        external_id = self._event_id_from_url(url)
        title = None
        starts = None
        venue_name = None
        city = None
        country = "IT"
        image_url = None

        raw: Dict[str, Any] = {"source_url": url}

        if jsonld:
            raw["jsonld"] = jsonld
            title = jsonld.get("name")
            starts = jsonld.get("startDate") or jsonld.get("start_date") or None
            image = jsonld.get("image")
            if isinstance(image, str):
                image_url = image
            elif isinstance(image, list) and image:
                if isinstance(image[0], str):
                    image_url = image[0]

            loc = jsonld.get("location")
            if isinstance(loc, dict):
                venue_name = loc.get("name") or venue_name
                addr = loc.get("address")
                if isinstance(addr, dict):
                    city = addr.get("addressLocality") or city
                    country = addr.get("addressCountry") or country

        # se manca title, prova da <title>
        if not title:
            soup = BeautifulSoup(html, "html.parser")
            t = (soup.title.string.strip() if soup.title and soup.title.string else "").strip()
            if t:
                title = t.split("|")[0].strip() or t

        if not title:
            return None

        return TicketSwapEvent(
            external_event_id=str(external_id),
            title=str(title),
            url=url,
            starts_at_iso=str(starts) if starts else None,
            venue_name=venue_name,
            city=city,
            country=country,
            image_url=image_url,
            raw=raw,
        )

    def fetch_events(self, limit: int = 50) -> List[TicketSwapEvent]:
        """
        Estrae fino a `limit` eventi dalla pagina location Italia.
        Nota: la pagina potrebbe caricare altri risultati via JS/paginazione.
        Per ora prendiamo quelli linkati nell'HTML (buono per partire).
        """
        html = self._get_html(self.LOCATION_ITALY)
        links = self._extract_event_links_from_location(html)

        out: List[TicketSwapEvent] = []
        for ev_url in links:
            if len(out) >= limit:
                break
            try:
                ev_html = self._get_html(ev_url)
                obj = self._build_event_from_page(ev_url, ev_html)
                if obj:
                    out.append(obj)
            except requests.RequestException as e:
                # salta senza bloccare tutto
                continue

        return out