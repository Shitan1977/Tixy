import random
import time
from typing import List

from .browser import TicketOneBrowser
from .client import TicketOneClient
from .parser import parse_event_detail, parse_event_links
from .schemas import TicketOneEventItem


# Per ora usiamo SOLO la sorgente stabile
DEFAULT_START_URLS = [
    ("https://www.ticketone.it/events/concerti-55/", "concerti"),
]


class TicketOneScraper:
    def __init__(self, verbose: bool = False):
        self.verbose = verbose
        self.client = TicketOneClient(verbose=verbose)

    def discover_events(self, limit: int = 20) -> List[TicketOneEventItem]:
        discovered: List[TicketOneEventItem] = []
        seen_urls = set()

        for start_url, category_hint in DEFAULT_START_URLS:
            if self.verbose:
                print(f"[DISCOVERY] start_url={start_url}")

            html = self.client.get_html(start_url)
            items = parse_event_links(html, start_url, category_hint=category_hint)

            if self.verbose:
                print(f"[DISCOVERY COUNT] url={start_url} found={len(items)}")

            # retry prudente se la pagina ha risposto ma il parser non ha trovato nulla
            if not items:
                if self.verbose:
                    print(f"[DISCOVERY RETRY] url={start_url}")

                time.sleep(random.uniform(4, 8))
                html = self.client.get_html(start_url)
                items = parse_event_links(html, start_url, category_hint=category_hint)

                if self.verbose:
                    print(f"[DISCOVERY COUNT RETRY] url={start_url} found={len(items)}")

            for item in items:
                if item.event_url in seen_urls:
                    continue

                seen_urls.add(item.event_url)
                discovered.append(item)

                if limit and len(discovered) >= limit:
                    return discovered

        return discovered

    def enrich_events(self, items: List[TicketOneEventItem]) -> List[TicketOneEventItem]:
        results: List[TicketOneEventItem] = []

        for idx, item in enumerate(items, start=1):
            if self.verbose:
                print(f"[DETAIL] {idx}/{len(items)} {item.event_url}")

            browser = TicketOneBrowser(headless=False, verbose=self.verbose)

            try:
                browser.start()
                html = browser.get_html(item.event_url)
                enriched_item = parse_event_detail(html, item)
                enriched_item.detail_status = "ok"
                results.append(enriched_item)

            except Exception as exc:
                if self.verbose:
                    print(f"[DETAIL ERROR] url={item.event_url} error={exc}")

                item.detail_status = "blocked"
                results.append(item)

            finally:
                try:
                    browser.stop()
                except Exception:
                    pass

            pause = random.uniform(8, 15)
            if self.verbose:
                print(f"[BETWEEN EVENTS SLEEP] {pause:.2f}s")
            time.sleep(pause)

        return results