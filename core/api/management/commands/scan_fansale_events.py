from django.core.management.base import BaseCommand

from api.scrapers.fansale_importer import run_import


class Command(BaseCommand):
    help = "Importa eventi fanSALE italiani non ancora presenti nel DB"

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit",
            type=int,
            default=0,
            help="Numero massimo di eventi da importare. 0 = nessun limite",
        )
        parser.add_argument(
            "--verbose",
            action="store_true",
            help="Abilita log dettagliati",
        )
        parser.add_argument(
            "--show-report",
            action="store_true",
            help="Mostra riepilogo per artista se disponibile",
        )

    def handle(self, *args, **options):
        limit = options["limit"]
        verbose = options["verbose"]
        show_report = options["show_report"]

        label_limit = "ALL" if limit == 0 else str(limit)

        self.stdout.write(
            self.style.WARNING(f"[START] scan_fansale_events limit={label_limit}")
        )

        stats = run_import(limit=limit, verbose=verbose)

        self.stdout.write(
            self.style.SUCCESS(
                f"[DONE] total={stats.get('total', 0)} "
                f"created={stats.get('created', 0)} "
                f"skipped_exists={stats.get('skipped_exists', 0)} "
                f"skipped_not_it={stats.get('skipped_not_it', 0)} "
                f"artists_total={stats.get('artists_total', 0)} "
                f"artists_with_events={stats.get('artists_with_events', 0)} "
                f"artists_blocked={stats.get('artists_blocked', 0)} "
                f"artists_empty={stats.get('artists_empty', 0)}"
            )
        )

        report = stats.get("artist_reports", [])
        if show_report and report:
            self.stdout.write("")
            self.stdout.write(self.style.WARNING("========== ARTIST REPORT =========="))

            for row in report:
                self.stdout.write(
                    f"[ARTIST] url={row.get('artist_url', '')} "
                    f"pages={row.get('pages_scanned', 0)} "
                    f"found={row.get('found_in_artist', 0)} "
                    f"created={row.get('created', 0)} "
                    f"skipped_exists={row.get('skipped_exists', 0)} "
                    f"blocked={row.get('blocked', False)} "
                    f"empty={row.get('empty', False)}"
                )