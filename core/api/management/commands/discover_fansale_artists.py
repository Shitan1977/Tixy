from __future__ import annotations

import os
import re
import time
from typing import List, Set

from bs4 import BeautifulSoup
from django.core.management.base import BaseCommand
from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.firefox.service import Service


DISCOVERY_PAGES = [
    "https://www.fansale.it/events/55",
    "https://www.fansale.it/events/new",

    "https://www.fansale.it/events/55/Pop",
    "https://www.fansale.it/events/55/Metal",
    "https://www.fansale.it/events/55/Jazz",
    "https://www.fansale.it/events/55/Musica",
]

SEED_FILE = "fansale_seed_artists.txt"


def get_firefox_binary():
    paths = [
        "/snap/firefox/current/usr/lib/firefox/firefox",
        "/usr/bin/firefox",
    ]
    for p in paths:
        if os.path.exists(p):
            return p
    raise RuntimeError("Firefox non trovato")


def build_driver():
    options = Options()
    options.add_argument("--headless")
    options.binary_location = get_firefox_binary()

    service = Service()

    driver = webdriver.Firefox(
        service=service,
        options=options,
    )
    driver.set_window_size(1600, 1200)
    return driver


def fetch_html_with_driver(driver, url: str, wait_seconds: int = 8) -> str:
    driver.get(url)
    time.sleep(3)

    try:
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(1)
        driver.execute_script("window.scrollTo(0, 0);")
        time.sleep(1)
    except Exception:
        pass

    end_time = time.time() + wait_seconds
    while time.time() < end_time:
        html = driver.page_source
        if "/tickets/all/" in html:
            return html
        time.sleep(1)

    return driver.page_source


def extract_artist_profile_urls(html: str) -> List[str]:
    """
    Estrae SOLO link profilo artista:
    /tickets/all/<slug>/<id>

    Esclude link offerta/evento:
    /tickets/all/<slug>/<id>/<offer_id>
    """
    soup = BeautifulSoup(html, "html.parser")
    found = []
    seen = set()

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()

        if not re.fullmatch(r"/tickets/all/[^/]+/\d+", href):
            continue

        full = "https://www.fansale.it" + href

        if full in seen:
            continue

        seen.add(full)
        found.append(full)

    return found


def load_existing_seed(filepath: str) -> Set[str]:
    existing = set()

    if not os.path.exists(filepath):
        return existing

    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            url = line.strip()
            if not url:
                continue
            url = url.split("#")[0].strip()
            existing.add(url)

    return existing


def append_new_seed_urls(filepath: str, urls: List[str]) -> int:
    existing = load_existing_seed(filepath)
    new_urls = []

    for url in urls:
        clean = url.split("#")[0].strip()
        if clean and clean not in existing:
            existing.add(clean)
            new_urls.append(clean)

    if not new_urls:
        return 0

    with open(filepath, "a", encoding="utf-8") as f:
        for url in new_urls:
            f.write(url + "\n")

    return len(new_urls)


class Command(BaseCommand):
    help = "Scopre URL artista fanSALE e li salva nel file seed locale"

    def add_arguments(self, parser):
        parser.add_argument("--pages", type=int, default=20)
        parser.add_argument("--wait", type=int, default=8)

    def handle(self, *args, **options):
        max_pages = options["pages"]
        wait_seconds = options["wait"]

        driver = build_driver()
        all_found = []
        seen = set()
        scanned = 0

        try:
            for base_url in DISCOVERY_PAGES:
                for page_num in range(1, max_pages + 1):
                    if "?page=" in base_url:
                        page_url = base_url
                    else:
                        page_url = base_url if page_num == 1 else f"{base_url}?page={page_num}"

                    self.stdout.write(f"[DISCOVERY PAGE] {page_url}")

                    try:
                        html = fetch_html_with_driver(driver, page_url, wait_seconds=wait_seconds)
                    except WebDriverException as e:
                        self.stdout.write(self.style.WARNING(f"[ERROR] {page_url} -> {e}"))
                        continue

                    scanned += 1
                    urls = extract_artist_profile_urls(html)
                    new_urls = [u for u in urls if u not in seen]

                    for u in new_urls:
                        seen.add(u)
                        all_found.append(u)

                    self.stdout.write(
                        f"[FOUND] matched={len(urls)} new={len(new_urls)} total_unique={len(all_found)}"
                    )

                    if len(urls) == 0 and page_num > 3:
                        # piccolo stop euristico
                        break

        finally:
            try:
                driver.quit()
            except Exception:
                pass

        written = append_new_seed_urls(SEED_FILE, all_found)

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(f"[SCANNED PAGES] {scanned}"))
        self.stdout.write(self.style.SUCCESS(f"[UNIQUE URLS FOUND] {len(all_found)}"))
        self.stdout.write(self.style.SUCCESS(f"[NEW URLS WRITTEN] {written}"))
        self.stdout.write(self.style.SUCCESS(f"[SEED FILE] {SEED_FILE}"))