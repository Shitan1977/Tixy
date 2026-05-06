from django.core.management.base import BaseCommand

from api.scrapers.ticketone.importer import import_ticketone_item, parse_starts_at, normalize_text
from api.scrapers.ticketone.runner import TicketOneScraper
from api.services.performance_matching import find_matching_performances


class Command(BaseCommand):
    help = "Scansiona TicketOne e importa gli eventi nel DB"

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=10)
        parser.add_argument("--verbose", action="store_true")
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument(
            "--match-dry-run",
            action="store_true",
            help="Mostra se gli eventi TicketOne avrebbero match con performance già esistenti, senza salvare nulla."
        )


    def handle(self, *args, **options):
        limit = options["limit"]
        verbose = options["verbose"]
        dry_run = options["dry_run"]
        match_dry_run = options["match_dry_run"]

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

            if match_dry_run:
                starts_at_utc = parse_starts_at(item.starts_at_raw)
                city = normalize_text(item.city)

                if not starts_at_utc:
                    self.stdout.write(
                        self.style.WARNING(
                            f"  [MATCH SKIP] data non valida title={item.title} raw_date={item.starts_at_raw}"
                        )
                    )
                else:
                    matches = find_matching_performances(
                        event_name=item.title,
                        starts_at_utc=starts_at_utc,
                        city=city or None,
                        hours_window=12,
                        min_similarity=0.80,
                    )

                    if matches:
                        best = matches[0]
                        p = best["performance"]

                        self.stdout.write(
                            self.style.SUCCESS(
                                f"  [MATCH FOUND] score={best['score']:.3f} "
                                f"existing_perf={p.id} existing_event={p.evento_id} "
                                f"existing_name={p.evento.nome_evento} "
                                f"existing_city={p.luogo.citta if p.luogo else '-'} "
                                f"existing_date={p.starts_at_utc}"
                            )
                        )
                    else:
                        self.stdout.write(
                            self.style.WARNING(
                                "  [MATCH MISS] nessuna performance esistente compatibile"
                            )
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