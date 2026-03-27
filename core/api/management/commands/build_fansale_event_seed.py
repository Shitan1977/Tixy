from __future__ import annotations

from django.core.management.base import BaseCommand

from api.models import EventoPiattaforma, Piattaforma


OUTPUT_FILE = "fansale_seed_artists.txt"


def artist_url_from_offer_url(url: str) -> str:
    """
    Da:
      https://www.fansale.it/tickets/all/geolier/577164/21304686
    a:
      https://www.fansale.it/tickets/all/geolier/577164
    """
    parts = url.rstrip("/").split("/")
    if len(parts) >= 7:
        return "/".join(parts[:7])
    return url.rstrip("/")


class Command(BaseCommand):
    help = "Genera il file fansale_seed_artists.txt dagli URL offerta fanSALE presenti nel DB"

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit",
            type=int,
            default=0,
            help="Numero massimo di artisti da esportare. 0 = tutti",
        )

    def handle(self, *args, **options):
        limit = options["limit"]

        try:
            piattaforma = Piattaforma.objects.get(nome="fansale")
        except Piattaforma.DoesNotExist:
            self.stdout.write(self.style.ERROR("Piattaforma fansale non trovata"))
            return

        qs = (
            EventoPiattaforma.objects
            .filter(piattaforma=piattaforma)
            .exclude(url="")
            .order_by("url")
        )

        seen = set()
        artist_urls = []

        for ep in qs:
            artist_url = artist_url_from_offer_url(ep.url)
            if artist_url not in seen:
                seen.add(artist_url)
                artist_urls.append(artist_url)

        if limit > 0:
            artist_urls = artist_urls[:limit]

        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            for url in artist_urls:
                f.write(url + "\n")

        self.stdout.write(
            self.style.SUCCESS(
                f"[DONE] scritto {len(artist_urls)} artisti in {OUTPUT_FILE}"
            )
        )