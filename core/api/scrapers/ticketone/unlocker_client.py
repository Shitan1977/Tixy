# api/scrapers/ticketone/unlocker_client.py
"""
Client Bright Data Web Unlocker per bypassare Akamai su TicketOne.
Usato sia per discovery (pagine lista) che per enrich (pagine dettaglio).
"""
import os
import requests


BRIGHTDATA_API_KEY = os.environ.get("BRIGHTDATA_API_KEY", "be81f746-fc93-4632-b4ae-5a86d7f39698")
BRIGHTDATA_ZONE = os.environ.get("BRIGHTDATA_ZONE", "web_unlocker1")


class TicketOneUnlockerClient:
    """
    Usa Bright Data Web Unlocker API per ottenere HTML di TicketOne
    bypassando il blocco Akamai.
    """

    def __init__(self, verbose: bool = False, timeout: int = 90):
        self.verbose = verbose
        self.timeout = timeout

    def get_html(self, url: str) -> str:
        if self.verbose:
            print(f"[UNLOCKER] GET {url}")

        response = requests.post(
            "https://api.brightdata.com/request",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {BRIGHTDATA_API_KEY}",
            },
            json={
                "zone": BRIGHTDATA_ZONE,
                "url": url,
                "format": "raw",
                "country": "it",
            },
            timeout=self.timeout,
        )

        if response.status_code != 200:
            raise Exception(f"Unlocker HTTP {response.status_code} for {url}")

        if self.verbose:
            print(f"[UNLOCKER] Got {len(response.text)} bytes")

        return response.text
