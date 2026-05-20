from urllib.parse import urlparse

from django.core.management.base import BaseCommand

from api.scrapers.ticketone.client import TicketOneClient
from api.scrapers.ticketone.importer import (
    import_ticketone_item,
    parse_starts_at,
    normalize_text,
)
from api.scrapers.ticketone.parser import (
    parse_event_links,
    extract_external_id,
)
from api.scrapers.ticketone.runner import TicketOneScraper
from api.scrapers.ticketone.schemas import TicketOneEventItem
from api.services.performance_matching import find_matching_performances


class Command(BaseCommand):
    help = "Scansione TicketOne avanzata: discovery, URL forzati, dettagli, prezzi e import DB"

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=20)
        parser.add_argument("--url", type=str, default=None, help="URL TicketOne forzato: pagina artista o pagina evento")
        parser.add_argument("--file", type=str, default=None, help="File con una lista di URL TicketOne, uno per riga")
        parser.add_argument("--verbose", action="store_true")
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument(
            "--match-dry-run",
            action="store_true",
            help="Mostra se gli eventi TicketOne avrebbero match con performance già esistenti, senza salvare nulla."
        )

    def _is_event_url(self, url: str) -> bool:
        parsed = urlparse(url)
        return "/event/" in parsed.path

    def _item_from_event_url(self, url: str) -> TicketOneEventItem:
        external_id = extract_external_id(url)

        # Titolo provvisorio: verrà corretto da parse_event_detail leggendo h1 dal dettaglio.
        slug = urlparse(url).path.rstrip("/").split("/")[-1]
        title = slug.replace("-", " ").title()

        return TicketOneEventItem(
            title=title or "Evento TicketOne",
            event_url=url,
            external_id=external_id,
            category_hint="concerti",
            source="forced_event_url",
            detail_status="not_attempted",
        )

    def _read_urls_from_file(self, path: str) -> list[str]:
        urls = []

        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()

                if not line:
                    continue

                if line.startswith("#"):
                    continue

                urls.append(line)

        return urls

    def _discover_from_forced_url(self, url: str, limit: int, verbose: bool) -> list[TicketOneEventItem]:
        """
        Gestisce due casi:
        1. URL diretto evento TicketOne:
           https://www.ticketone.it/event/...
        2. URL pagina artista TicketOne:
           https://www.ticketone.it/artist/ultimo/?affiliate=IGA
        """

        if self._is_event_url(url):
            if verbose:
                self.stdout.write(self.style.WARNING(f"[FORCED EVENT URL] {url}"))

            return [self._item_from_event_url(url)]

        if verbose:
            self.stdout.write(self.style.WARNING(f"[FORCED PAGE URL] {url}"))

        client = TicketOneClient(verbose=verbose)
        html = client.get_html(url)

        items = parse_event_links(
            html=html,
            base_url=url,
            category_hint="concerti",
        )

        if verbose:
            self.stdout.write(self.style.SUCCESS(f"[FORCED PAGE FOUND] {len(items)} eventi trovati da {url}"))

        if limit:
            items = items[:limit]

        return items

    def _dedupe_items(self, items: list[TicketOneEventItem]) -> list[TicketOneEventItem]:
        unique = []
        seen = set()

        for item in items:
            key = item.event_url

            if key in seen:
                continue

            seen.add(key)
            unique.append(item)

        return unique

    def _print_match_info(self, item: TicketOneEventItem):
        starts_at_utc = parse_starts_at(item.starts_at_raw)
        city = normalize_text(item.city)

        if not starts_at_utc:
            self.stdout.write(
                self.style.WARNING(
                    f"  [MATCH SKIP] data non valida title={item.title} raw_date={item.starts_at_raw}"
                )
            )
            return

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

    def handle(self, *args, **options):
        limit = options["limit"]
        forced_url = options["url"]
        file_path = options["file"]
        verbose = options["verbose"]
        dry_run = options["dry_run"]
        match_dry_run = options["match_dry_run"]

        scraper = TicketOneScraper(verbose=verbose)

        self.stdout.write(
            self.style.WARNING(
                f"[START] scan_ticketone_details_full limit={limit} dry_run={dry_run}"
            )
        )

        discovered = []

        # Caso 1: URL singolo forzato.
        if forced_url:
            discovered.extend(
                self._discover_from_forced_url(
                    url=forced_url,
                    limit=limit,
                    verbose=verbose,
                )
            )

        # Caso 2: file con URL forzati.
        elif file_path:
            urls = self._read_urls_from_file(file_path)

            self.stdout.write(
                self.style.WARNING(
                    f"[FORCED FILE] {file_path} urls={len(urls)}"
                )
            )

            for url in urls:
                items = self._discover_from_forced_url(
                    url=url,
                    limit=limit,
                    verbose=verbose,
                )
                discovered.extend(items)

                if limit and len(discovered) >= limit:
                    discovered = discovered[:limit]
                    break

        # Caso 3: discovery standard.
        else:
            discovered = scraper.discover_events(limit=limit)

        discovered = self._dedupe_items(discovered)

        self.stdout.write(
            self.style.SUCCESS(
                f"[DISCOVERED] {len(discovered)} eventi trovati"
            )
        )

        if not discovered:
            self.stdout.write(self.style.WARNING("[STOP] nessun evento trovato"))
            return

        results = scraper.enrich_events(discovered)

        self.stdout.write(
            self.style.SUCCESS(
                f"[RESULTS] {len(results)} eventi processati"
            )
        )

        ok_count = sum(1 for x in results if x.detail_status == "ok")
        blocked_count = sum(1 for x in results if x.detail_status == "blocked")

        self.stdout.write(self.style.SUCCESS(f"[DETAIL OK] {ok_count}"))
        self.stdout.write(self.style.WARNING(f"[DETAIL BLOCKED] {blocked_count}"))

        imported = 0
        skipped_extra = 0
        skipped_location = 0
        skipped_blocked = 0

        for item in results:
            self.stdout.write(
                f"- title={item.title} | city={item.city} | venue={item.venue} | "
                f"date={item.starts_at_raw} | price={item.price_text} | "
                f"external_id={item.external_id} | detail_status={item.detail_status} | "
                f"source={item.source} | url={item.event_url}"
            )

            # Se il dettaglio è bloccato, non importiamo.
            if item.detail_status != "ok":
                skipped_blocked += 1
                self.stdout.write(
                    self.style.WARNING(
                        f"[SKIP BLOCKED] title={item.title} external_id={item.external_id}"
                    )
                )
                continue

            title_low = (item.title or "").lower()

            # Evitiamo di importare pacchetti, parcheggi, VIP, premium ecc.
            skip_keywords = [
                "package",
                "vip",
                "premium",
                "party terrace",
                "parcheggio",
                "parking",
            ]

            if any(keyword in title_low for keyword in skip_keywords):
                skipped_extra += 1
                self.stdout.write(
                    self.style.WARNING(
                        f"[SKIP EXTRA] title={item.title} external_id={item.external_id}"
                    )
                )
                continue

            # Evitiamo eventi senza città o venue, perché rischiano di creare performance sporche.
            if not item.city or not item.venue:
                skipped_location += 1
                self.stdout.write(
                    self.style.WARNING(
                        f"[SKIP LOCATION MISSING] title={item.title} "
                        f"city={item.city} venue={item.venue} external_id={item.external_id}"
                    )
                )
                continue

            if match_dry_run:
                self._print_match_info(item)

            # In dry-run arriviamo fin qui: vediamo cosa verrebbe importato,
            # ma non salviamo nulla nel DB.
            if dry_run:
                self.stdout.write(
                    self.style.SUCCESS(
                        f"[DRY RUN OK] importabile title={item.title} external_id={item.external_id}"
                    )
                )
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

        self.stdout.write(
            self.style.WARNING(
                f"[SUMMARY] imported={imported} "
                f"skipped_extra={skipped_extra} "
                f"skipped_location={skipped_location} "
                f"skipped_blocked={skipped_blocked} "
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