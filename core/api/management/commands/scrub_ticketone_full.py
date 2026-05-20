import time
import random
from urllib.parse import urljoin

from django.core.management.base import BaseCommand

from api.scrapers.ticketone.client import TicketOneClient
from api.scrapers.ticketone.importer import import_ticketone_item
from api.scrapers.ticketone.parser import parse_event_links
from api.scrapers.ticketone.runner import TicketOneScraper


class Command(BaseCommand):
    help = "Scrub automatico TicketOne: scopre eventi, apre dettagli, pulisce e importa nel DB"

    START_URLS = [
        "https://www.ticketone.it/events/concerti-55/",
    ]

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=100)
        parser.add_argument("--pages", type=int, default=1)
        parser.add_argument("--verbose", action="store_true")
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument(
            "--sleep-min",
            type=float,
            default=4.0,
            help="Pausa minima tra pagine discovery"
        )
        parser.add_argument(
            "--sleep-max",
            type=float,
            default=8.0,
            help="Pausa massima tra pagine discovery"
        )

    def _build_page_urls(self, pages: int) -> list[str]:
        """
        Costruisce una lista prudente di URL TicketOne da cui partire.

        Per ora usiamo la pagina concerti principale.
        Se TicketOne accetta ?page=N, proviamo anche pagine successive.
        """
        urls = []

        for base_url in self.START_URLS:
            urls.append(base_url)

            for page in range(2, pages + 1):
                urls.append(f"{base_url}?page={page}")

        return urls

    def _is_extra_event(self, item) -> bool:
        """
        Scarta eventi che non sono concerti veri:
        package, VIP, premium, parcheggi, party terrace ecc.
        """
        title_low = (item.title or "").lower()
        url_low = (item.event_url or "").lower()

        skip_keywords = [
            "package",
            "vip",
            "premium",
            "party terrace",
            "parcheggio",
            "parking",
            "reservation",
            "abbonamento",
            "full pass",
            "august pass",
        ]

        text = f"{title_low} {url_low}"

        return any(keyword in text for keyword in skip_keywords)

    def _has_valid_location(self, item) -> bool:
        """
        Importiamo solo eventi con città e venue.
        Serve per evitare performance sporche.
        """
        return bool(item.city and item.venue)

    def _dedupe_items(self, items):
        unique = []
        seen = set()

        for item in items:
            key = item.external_id or item.event_url

            if key in seen:
                continue

            seen.add(key)
            unique.append(item)

        return unique

    def _sleep_between_pages(self, sleep_min: float, sleep_max: float, verbose: bool):
        pause = random.uniform(sleep_min, sleep_max)

        if verbose:
            self.stdout.write(
                self.style.WARNING(
                    f"[DISCOVERY SLEEP] {pause:.2f}s"
                )
            )

        time.sleep(pause)

    def handle(self, *args, **options):
        limit = options["limit"]
        pages = options["pages"]
        verbose = options["verbose"]
        dry_run = options["dry_run"]
        sleep_min = options["sleep_min"]
        sleep_max = options["sleep_max"]

        self.stdout.write(
            self.style.WARNING(
                f"[START] scrub_ticketone_full "
                f"limit={limit} pages={pages} dry_run={dry_run}"
            )
        )

        client = TicketOneClient(verbose=verbose)
        scraper = TicketOneScraper(verbose=verbose)

        discovered = []
        page_urls = self._build_page_urls(pages)

        self.stdout.write(
            self.style.WARNING(
                f"[DISCOVERY URLS] {len(page_urls)} pagine da controllare"
            )
        )

        for idx, page_url in enumerate(page_urls, start=1):
            if limit and len(discovered) >= limit:
                break

            self.stdout.write(
                self.style.WARNING(
                    f"[DISCOVERY PAGE] {idx}/{len(page_urls)} {page_url}"
                )
            )

            try:
                html = client.get_html(page_url)
                items = parse_event_links(
                    html=html,
                    base_url=page_url,
                    category_hint="concerti",
                )

                self.stdout.write(
                    self.style.SUCCESS(
                        f"[DISCOVERY FOUND] url={page_url} found={len(items)}"
                    )
                )

                discovered.extend(items)
                discovered = self._dedupe_items(discovered)

                if limit and len(discovered) >= limit:
                    discovered = discovered[:limit]
                    break

            except Exception as exc:
                self.stdout.write(
                    self.style.ERROR(
                        f"[DISCOVERY ERROR] url={page_url} error={exc}"
                    )
                )

            self._sleep_between_pages(
                sleep_min=sleep_min,
                sleep_max=sleep_max,
                verbose=verbose,
            )

        discovered = self._dedupe_items(discovered)

        if limit:
            discovered = discovered[:limit]

        self.stdout.write(
            self.style.SUCCESS(
                f"[DISCOVERED TOTAL] {len(discovered)} eventi unici trovati"
            )
        )

        if not discovered:
            self.stdout.write(
                self.style.WARNING("[STOP] nessun evento trovato")
            )
            return

        results = scraper.enrich_events(discovered)

        self.stdout.write(
            self.style.SUCCESS(
                f"[RESULTS] {len(results)} eventi processati"
            )
        )

        ok_count = sum(1 for item in results if item.detail_status == "ok")
        blocked_count = sum(1 for item in results if item.detail_status == "blocked")

        self.stdout.write(self.style.SUCCESS(f"[DETAIL OK] {ok_count}"))
        self.stdout.write(self.style.WARNING(f"[DETAIL BLOCKED] {blocked_count}"))

        imported = 0
        skipped_extra = 0
        skipped_location = 0
        skipped_blocked = 0
        failed_import = 0

        for item in results:
            self.stdout.write(
                f"- title={item.title} | city={item.city} | venue={item.venue} | "
                f"date={item.starts_at_raw} | price={item.price_text} | "
                f"external_id={item.external_id} | detail_status={item.detail_status} | "
                f"source={item.source} | url={item.event_url}"
            )

            if item.detail_status != "ok":
                skipped_blocked += 1
                self.stdout.write(
                    self.style.WARNING(
                        f"[SKIP BLOCKED] title={item.title} external_id={item.external_id}"
                    )
                )
                continue

            if self._is_extra_event(item):
                skipped_extra += 1
                self.stdout.write(
                    self.style.WARNING(
                        f"[SKIP EXTRA] title={item.title} external_id={item.external_id}"
                    )
                )
                continue

            if not self._has_valid_location(item):
                skipped_location += 1
                self.stdout.write(
                    self.style.WARNING(
                        f"[SKIP LOCATION MISSING] title={item.title} "
                        f"city={item.city} venue={item.venue} "
                        f"external_id={item.external_id}"
                    )
                )
                continue

            if dry_run:
                self.stdout.write(
                    self.style.SUCCESS(
                        f"[DRY RUN OK] importabile title={item.title} "
                        f"external_id={item.external_id}"
                    )
                )
                continue

            try:
                outcome = import_ticketone_item(item)
                imported += 1

                if verbose:
                    self.stdout.write(
                        self.style.SUCCESS(
                            f"[IMPORTED] evento_id={outcome['evento_id']} "
                            f"performance_id={outcome['performance_id']} "
                            f"detail_status={outcome['detail_status']}"
                        )
                    )

            except Exception as exc:
                failed_import += 1
                self.stdout.write(
                    self.style.ERROR(
                        f"[IMPORT ERROR] title={item.title} "
                        f"external_id={item.external_id} error={exc}"
                    )
                )

        self.stdout.write(
            self.style.WARNING(
                f"[SUMMARY] imported={imported} "
                f"skipped_extra={skipped_extra} "
                f"skipped_location={skipped_location} "
                f"skipped_blocked={skipped_blocked} "
                f"failed_import={failed_import} "
                f"dry_run={dry_run}"
            )
        )

        if not dry_run:
            self.stdout.write(
                self.style.SUCCESS(
                    f"[DB IMPORTED] {imported} eventi"
                )
            )

        self.stdout.write(self.style.SUCCESS("[DONE]"))