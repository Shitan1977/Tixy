from django.core.management.base import BaseCommand

from api.scrapers.fansale_price_importer import run_price_import


class Command(BaseCommand):
    help = "Aggiorna i prezzi fanSALE dalle pagine artista, in modo separato e prudente"

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit-artists",
            type=int,
            default=0,
            help="Numero massimo di artisti da processare. 0 = nessun limite",
        )
        parser.add_argument(
            "--verbose",
            action="store_true",
            help="Abilita log dettagliati",
        )
        parser.add_argument(
            "--show-report",
            action="store_true",
            help="Mostra riepilogo per artista",
        )

    def handle(self, *args, **options):
        limit_artists = options["limit_artists"]
        verbose = options["verbose"]
        show_report = options["show_report"]

        label_limit = "ALL" if limit_artists == 0 else str(limit_artists)

        self.stdout.write(
            self.style.WARNING(f"[START] scan_fansale_prices_safe limit_artists={label_limit}")
        )

        stats = run_price_import(limit_artists=limit_artists, verbose=verbose)

        self.stdout.write(
            self.style.SUCCESS(
                f"[DONE] total={stats.get('total', 0)} "
                f"updated={stats.get('updated', 0)} "
                f"not_in_db={stats.get('not_in_db', 0)} "
                f"price_not_found={stats.get('price_not_found', 0)} "
                f"no_performance={stats.get('no_performance', 0)} "
                f"artists_total={stats.get('artists_total', 0)} "
                f"artists_with_prices={stats.get('artists_with_prices', 0)} "
                f"artists_blocked={stats.get('artists_blocked', 0)} "
                f"artists_empty={stats.get('artists_empty', 0)}"
            )
        )

        report = stats.get("artist_reports", [])
        if show_report and report:
            self.stdout.write("")
            self.stdout.write(self.style.WARNING("========== PRICE ARTIST REPORT =========="))

            for row in report:
                self.stdout.write(
                    f"[ARTIST] url={row.get('artist_url', '')} "
                    f"found={row.get('found_in_artist', 0)} "
                    f"blocked={row.get('blocked', False)} "
                    f"empty={row.get('empty', False)}"
                )