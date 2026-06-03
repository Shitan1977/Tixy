# api/management/commands/scrub_vivaticket_events.py

import time
from api.scrapers.vivaticket.importer import import_vivaticket_event
from django.core.management.base import BaseCommand

from api.scrapers.vivaticket.browser import fetch_vivaticket_music_page
from api.scrapers.vivaticket.parser import parse_vivaticket_events
from api.scrapers.vivaticket.client import get_vivaticket_event_detail


class Command(BaseCommand):
    help = "Scraper locale Vivaticket eventi: lista + arricchimento API evento"

    def add_arguments(self, parser):
        parser.add_argument(
            "--url",
            type=str,
            default="https://www.vivaticket.com/it/biglietti-musica/10",
            help="URL Vivaticket da leggere",
        )

        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Esegue il test senza importare nulla nel database",
        )

        parser.add_argument(
            "--verbose",
            action="store_true",
            help="Mostra output dettagliato",
        )

        parser.add_argument(
            "--headless",
            action="store_true",
            help="Esegue Chromium in modalità headless",
        )

        parser.add_argument(
            "--limit",
            type=int,
            default=20,
            help="Numero massimo di eventi da processare",
        )

        parser.add_argument(
            "--sleep",
            type=float,
            default=0.5,
            help="Pausa tra una chiamata API e l'altra",
        )

    def handle(self, *args, **options):
        url = options["url"]
        dry_run = options["dry_run"]
        verbose = options["verbose"]
        headless = options["headless"]
        limit = options["limit"]
        sleep_seconds = options["sleep"]

        self.stdout.write("[START] scrub_vivaticket_events")
        self.stdout.write(f"[URL] {url}")
        self.stdout.write(f"[DRY RUN] {dry_run}")
        self.stdout.write(f"[HEADLESS] {headless}")
        self.stdout.write(f"[LIMIT] {limit}")
        self.stdout.write(f"[SLEEP] {sleep_seconds}")

        html = fetch_vivaticket_music_page(
            url=url,
            headless=headless,
        )

        self.stdout.write(f"[HTML] length={len(html)}")

        list_events = parse_vivaticket_events(html)

        self.stdout.write(f"[LIST EVENTS FOUND] {len(list_events)}")

        processed = 0
        enriched = 0
        failed = 0
        skipped_no_external_id = 0

        for list_event in list_events:
            if processed >= limit:
                break

            external_id = list_event.external_id
            source_url = list_event.url

            if not external_id:
                skipped_no_external_id += 1
                continue

            processed += 1

            self.stdout.write("")
            self.stdout.write("=" * 80)
            self.stdout.write(f"[EVENT #{processed}] external_id={external_id}")
            self.stdout.write(f"source_url={source_url}")

            event = get_vivaticket_event_detail(
                event_id=external_id,
                source_url=source_url,
            )

            if not event:
                failed += 1
                self.stdout.write("[API RESULT] NO DATA")
                time.sleep(sleep_seconds)
                continue

            enriched += 1

            self.stdout.write(f"title={event.get('title')}")
            self.stdout.write(f"subtitle={event.get('subtitle')}")
            self.stdout.write(f"date={event.get('starts_at_raw')}")
            self.stdout.write(f"raw_date={event.get('raw_date')}")
            self.stdout.write(f"city={event.get('city')}")
            self.stdout.write(f"venue={event.get('venue')}")
            self.stdout.write(f"province={event.get('province')}")
            self.stdout.write(f"address={event.get('address')}")
            self.stdout.write(f"organizer={event.get('organizer')}")

            self.stdout.write(f"performance_id={event.get('performance_id')}")
            self.stdout.write(f"performance_code={event.get('performance_code')}")
            self.stdout.write(f"pcode={event.get('pcode')}")
            self.stdout.write(f"tcode={event.get('tcode')}")
            self.stdout.write(f"performance_status={event.get('performance_status')}")
            self.stdout.write(f"is_sell_active={event.get('is_sell_active')}")
            self.stdout.write(f"sale_status={event.get('sale_status')}")
            self.stdout.write(f"shop_type={event.get('shop_type')}")
            self.stdout.write(f"shop_url={event.get('shop_url')}")

            if verbose:
                self.stdout.write(f"dates={event.get('dates')}")
                self.stdout.write(f"hours={event.get('hours')}")

            import_result = import_vivaticket_event(
                event_data=event,
                external_id=external_id,
                dry_run=dry_run,
            )

            self.stdout.write(f"[IMPORT RESULT] {import_result}")

            time.sleep(sleep_seconds)

        self.stdout.write("")
        self.stdout.write("[SUMMARY]")
        self.stdout.write(f"processed={processed}")
        self.stdout.write(f"enriched={enriched}")
        self.stdout.write(f"failed={failed}")
        self.stdout.write(f"skipped_no_external_id={skipped_no_external_id}")
        self.stdout.write("[END] scrub_vivaticket_events")