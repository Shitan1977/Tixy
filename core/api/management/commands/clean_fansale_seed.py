from __future__ import annotations

import os
import re
from typing import List, Set

from django.core.management.base import BaseCommand


DEFAULT_SEED_FILE = "fansale_seed_artists.txt"


def normalize_artist_url(url: str) -> str:
    return url.split("#")[0].strip()


def is_valid_artist_url(url: str) -> bool:
    return re.fullmatch(r"https://www\.fansale\.it/tickets/all/[^/]+/\d+", url) is not None


def load_urls(filepath: str) -> List[str]:
    if not os.path.exists(filepath):
        return []

    urls = []
    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            raw = line.strip()
            if not raw:
                continue
            urls.append(raw)
    return urls


def clean_urls(urls: List[str]) -> List[str]:
    cleaned: Set[str] = set()

    for url in urls:
        url = normalize_artist_url(url)
        if not url:
            continue
        if not is_valid_artist_url(url):
            continue
        cleaned.add(url)

    return sorted(cleaned)


class Command(BaseCommand):
    help = "Pulisce e deduplica il file fansale_seed_artists.txt"

    def add_arguments(self, parser):
        parser.add_argument(
            "--file",
            type=str,
            default=DEFAULT_SEED_FILE,
            help="Percorso del file seed da pulire",
        )
        parser.add_argument(
            "--write",
            action="store_true",
            help="Scrive il risultato nel file",
        )

    def handle(self, *args, **options):
        filepath = options["file"]
        write = options["write"]

        original_urls = load_urls(filepath)
        cleaned_urls = clean_urls(original_urls)

        removed_count = len(original_urls) - len(cleaned_urls)

        self.stdout.write(f"[FILE] {filepath}")
        self.stdout.write(f"[ORIGINAL ROWS] {len(original_urls)}")
        self.stdout.write(f"[VALID UNIQUE ROWS] {len(cleaned_urls)}")
        self.stdout.write(f"[REMOVED/INVALID/DUPLICATE] {removed_count}")
        self.stdout.write("")

        for url in cleaned_urls:
            self.stdout.write(url)

        if write:
            with open(filepath, "w", encoding="utf-8") as f:
                for url in cleaned_urls:
                    f.write(url + "\n")

            self.stdout.write("")
            self.stdout.write(self.style.SUCCESS(f"[WRITTEN] {filepath}"))