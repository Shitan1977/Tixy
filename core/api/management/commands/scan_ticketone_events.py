from django.core.management.base import BaseCommand

from api.scrapers.ticketone.importer import import_ticketone_item
from api.scrapers.ticketone.runner import TicketOneScraper


class Command(BaseCommand):
    help = "Scansiona TicketOne e importa gli eventi nel DB"

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=10)
        parser.add_argument("--verbose", action="store_true")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        limit = options["limit"]
        verbose = options["verbose"]
        dry_run = options["dry_run"]

        scraper = TicketOneScraper(verbose=verbose)

        self.stdout.write(self.style.WARNING(f"[START] scan_ticketone_events limit={limit}"))

        discovered = scraper.discover_events(limit=limit)
        self.stdout.write(self.style.SUCCESS(f"[DISCOVERED] {len(discovered)} eventi trovati"))

        results = scraper.enrich_events(discovered)
        self.stdout.write(self.style.SUCCESS(f"[RESULTS] {len(results)} eventi processati"))

        ok_count = sum(1 for x in results if x.detail_status == "ok")
        blocked_count = sum(1 for x in results if x.detail_status == "blocked")
        self.stdout.write(self.style.SUCCESS(f"[DETAIL OK] {ok_count}"))
        self.stdout.write(self.style.WARNING(f"[DETAIL BLOCKED] {blocked_count}"))

        imported = 0

        for item in results:
            self.stdout.write(
                f"- title={item.title} | city={item.city} | venue={item.venue} | "
                f"date={item.starts_at_raw} | price={item.price_text} | "
                f"external_id={item.external_id} | detail_status={item.detail_status} | source={item.source}"
            )

            if dry_run:
                continue

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

        if not dry_run:
            self.stdout.write(self.style.SUCCESS(f"[DB IMPORTED] {imported} eventi"))

        self.stdout.write(self.style.SUCCESS("[DONE]"))