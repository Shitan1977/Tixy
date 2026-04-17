# core/api/scrapers/ticketone/client.py
import random
import time
from typing import Optional

import requests


class TicketOneClient:
    """
    Client HTTP 'umano':
    - sessione persistente
    - header realistici
    - pause casuali
    - retry moderati
    """

    BASE_HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:137.0) "
            "Gecko/20100101 Firefox/137.0"
        ),
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;"
            "q=0.9,image/avif,image/webp,*/*;q=0.8"
        ),
        "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "DNT": "1",
    }

    def __init__(
        self,
        timeout: int = 25,
        min_sleep: float = 2.5,
        max_sleep: float = 5.5,
        max_retries: int = 2,
        verbose: bool = False,
    ):
        self.timeout = timeout
        self.min_sleep = min_sleep
        self.max_sleep = max_sleep
        self.max_retries = max_retries
        self.verbose = verbose
        self.session = requests.Session()
        self.session.headers.update(self.BASE_HEADERS)

    def _sleep(self, low: Optional[float] = None, high: Optional[float] = None) -> None:
        low = self.min_sleep if low is None else low
        high = self.max_sleep if high is None else high
        seconds = random.uniform(low, high)
        if self.verbose:
            print(f"[SLEEP] {seconds:.2f}s")
        time.sleep(seconds)

    def get_html(self, url: str, referer: Optional[str] = None, allow_sleep: bool = True) -> str:
        headers = {}
        if referer:
            headers["Referer"] = referer

        last_error = None

        for attempt in range(1, self.max_retries + 2):
            try:
                if self.verbose:
                    print(f"[GET] attempt={attempt} url={url}")

                response = self.session.get(
                    url,
                    headers=headers,
                    timeout=self.timeout,
                    allow_redirects=True,
                )

                status = response.status_code
                if self.verbose:
                    print(f"[STATUS] {status} url={url}")

                # 200 OK
                if status == 200:
                    if allow_sleep:
                        self._sleep()
                    return response.text

                # Errori 'sensibili'
                if status in (403, 429, 500, 502, 503, 504):
                    last_error = RuntimeError(f"HTTP {status} for {url}")
                    backoff = min(20, attempt * random.uniform(3.0, 6.0))
                    if self.verbose:
                        print(f"[BACKOFF] {backoff:.2f}s dopo HTTP {status}")
                    time.sleep(backoff)
                    continue

                # altri status
                response.raise_for_status()

            except requests.RequestException as exc:
                last_error = exc
                backoff = min(20, attempt * random.uniform(2.0, 5.0))
                if self.verbose:
                    print(f"[ERROR] {exc} -> sleep {backoff:.2f}s")
                time.sleep(backoff)

        raise RuntimeError(f"Fetch fallito per {url}: {last_error}")