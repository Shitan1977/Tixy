from django.core.management.base import BaseCommand

from api.scrapers.fansale_importer import run_import


class Command(BaseCommand):
    help = "Importa eventi fanSALE italiani non ancora presenti nel DB"

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=100)
        parser.add_argument("--verbose", action="store_true")

    def handle(self, *args, **options):
        limit = options["limit"]
        verbose = options["verbose"]

        self.stdout.write(self.style.WARNING(
            f"[START] scan_fansale_events limit={limit}"
        ))

        stats = run_import(limit=limit, verbose=verbose)

        self.stdout.write(self.style.SUCCESS(
            f"[DONE] total={stats['total']} created={stats['created']} "
            f"skipped_exists={stats['skipped_exists']} "
            f"skipped_not_it={stats['skipped_not_it']}"
        ))