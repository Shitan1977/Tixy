from __future__ import annotations

import os
import re
import time
from typing import List

from bs4 import BeautifulSoup
from django.core.management.base import BaseCommand
from selenium import webdriver
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.firefox.service import Service


SEED_FILE = "fansale_seed_artists.txt"

def detect_access_denied(html: str, title: str) -> bool:
    text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True).lower()
    title_l = (title or "").strip().lower()

    signals = [
        "access denied",
        "you don't have permission",
        "forbidden",
        "blocked",
    ]

    if any(s in title_l for s in signals):
        return True

    if any(s in text for s in signals):
        return True

    return False

def get_firefox_binary() -> str:
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


def normalize_artist_url(url: str) -> str:
    return url.split("#")[0].strip()


def is_valid_artist_url(url: str) -> bool:
    return re.fullmatch(r"https://www\.fansale\.it/tickets/all/[^/]+/\d+", url) is not None


def load_seed_urls(filepath: str) -> List[str]:
    if not os.path.exists(filepath):
        return []

    urls = []
    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            url = normalize_artist_url(line.strip())
            if not url:
                continue
            if is_valid_artist_url(url):
                urls.append(url)

    # deduplica mantenendo ordine
    seen = set()
    ordered = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            ordered.append(u)

    return ordered


def fetch_html(driver, url: str, wait_seconds: int = 8) -> str:
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


def extract_page_title(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.string.strip() if soup.title and soup.title.string else ""
    return title


def count_offer_links(html: str) -> int:
    soup = BeautifulSoup(html, "html.parser")
    count = 0

    for a in soup.find_all("a", href=True):
        href = a.get("href", "").strip()
        if re.fullmatch(r"/tickets/all/[^/]+/\d+/\d+", href):
            count += 1

    return count


class Command(BaseCommand):
    help = "Valida gli URL artista di fansale_seed_artists.txt"

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=0)
        parser.add_argument("--wait", type=int, default=8)

    def handle(self, *args, **options):
        limit = options["limit"]
        wait_seconds = options["wait"]

        urls = load_seed_urls(SEED_FILE)
        if limit > 0:
            urls = urls[:limit]

        if not urls:
            self.stdout.write(self.style.WARNING("[EMPTY] Nessun URL seed valido trovato"))
            return

        driver = None
        ok_count = 0
        empty_count = 0
        error_count = 0

        try:
            driver = build_driver()

            for idx, url in enumerate(urls, start=1):
                self.stdout.write(f"\n[{idx}/{len(urls)}] {url}")

                try:
                    html = fetch_html(driver, url, wait_seconds=wait_seconds)
                except Exception as e:
                    error_count += 1
                    self.stdout.write(self.style.WARNING(f"[ERROR] {e}"))
                    continue

                title = extract_page_title(html)
                offer_count = count_offer_links(html)

                if detect_access_denied(html, title):
                    error_count += 1
                    self.stdout.write(self.style.WARNING(f"[ACCESS_DENIED] title={title}"))
                elif offer_count > 0:
                    ok_count += 1
                    self.stdout.write(self.style.SUCCESS(f"[OK] offers={offer_count} title={title}"))
                else:
                    empty_count += 1
                    self.stdout.write(self.style.WARNING(f"[EMPTY] offers=0 title={title}"))

        finally:
            if driver is not None:
                try:
                    driver.quit()
                except Exception:
                    pass

        self.stdout.write("\n========== SUMMARY ==========")
        self.stdout.write(self.style.SUCCESS(f"[OK URLS] {ok_count}"))
        self.stdout.write(self.style.WARNING(f"[EMPTY URLS] {empty_count}"))
        self.stdout.write(self.style.WARNING(f"[ERROR URLS] {error_count}"))
        self.stdout.write(f"[TOTAL CHECKED] {len(urls)}")