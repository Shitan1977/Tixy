from __future__ import annotations

from typing import Optional, Dict, Any, List
import requests


class EventbriteClient:
    BASE = "https://www.eventbriteapi.com/v3"

    def __init__(self, token: str, timeout: int = 25):
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        })
        self._venue_cache: Dict[str, Dict[str, Any]] = {}

    def _get(self, path: str, params: Optional[dict] = None) -> dict:
        url = f"{self.BASE}{path}"
        r = self.session.get(url, params=params or {}, timeout=self.timeout)
        if r.status_code != 200:
            raise RuntimeError(f"Eventbrite HTTP {r.status_code} on {url}: {r.text[:300]}")
        return r.json()

    def get_venue(self, venue_id: str) -> Optional[dict]:
        if not venue_id:
            return None
        if venue_id in self._venue_cache:
            return self._venue_cache[venue_id]
        data = self._get(f"/venues/{venue_id}/")
        self._venue_cache[venue_id] = data
        return data

    def fetch_org_events(self, org_id: str, status: str = "all", page_size: int = 50) -> List[dict]:
        """
        Ritorna lista di dict "normalizzati" per upsert nel tuo DB.
        Paginazione gestita via continuation.
        Venue risolta via venue_id con cache.
        """
        path = f"/organizations/{org_id}/events/"
        params = {"status": status, "page_size": page_size}

        out: List[dict] = []
        continuation = None

        while True:
            if continuation:
                params["continuation"] = continuation
            else:
                params.pop("continuation", None)

            data = self._get(path, params=params)
            events = data.get("events", []) or []

            for ev in events:
                venue_name = city = country = None

                venue_id = ev.get("venue_id")
                if venue_id:
                    v = self.get_venue(str(venue_id)) or {}
                    venue_name = v.get("name")
                    addr = v.get("address") or {}
                    city = addr.get("city")
                    country = addr.get("country")

                out.append({
                    "external_event_id": str(ev.get("id") or ""),
                    "title": (ev.get("name") or {}).get("text") or "",
                    "starts_at_iso": (ev.get("start") or {}).get("utc"),
                    "ends_at_iso": (ev.get("end") or {}).get("utc"),
                    "venue_id": str(venue_id) if venue_id else None,
                    "venue_name": venue_name,
                    "city": city,
                    "country": country,
                    "url": ev.get("url"),
                    "currency": ev.get("currency"),
                    "status": ev.get("status"),
                    "raw": ev,
                })

            pag = data.get("pagination", {}) or {}
            continuation = pag.get("continuation")
            has_more = bool(pag.get("has_more_items"))

            if not has_more or not continuation:
                break

        return out
