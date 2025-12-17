import os
import requests

TM_BASE = "https://app.ticketmaster.com/discovery/v2/events.json"

def fetch_events(page_size=20, country_code="IT"):
    api_key = os.getenv("TICKETMASTER_API_KEY")
    if not api_key:
        raise RuntimeError("Missing env var TICKETMASTER_API_KEY")

    params = {
        "apikey": api_key,
        "size": page_size,
        "countryCode": country_code,
        "sort": "date,asc",
    }

    r = requests.get(TM_BASE, params=params, timeout=25)
    r.raise_for_status()
    return r.json()
