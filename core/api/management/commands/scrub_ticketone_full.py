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
        # Pagina principale concerti
        "https://www.ticketone.it/events/concerti-55/",
        # Sottocategorie concerti
        "https://www.ticketone.it/events/pop-rock-84/",
        "https://www.ticketone.it/events/festival-87/",
        "https://www.ticketone.it/events/hip-hop-rap-98/",
        "https://www.ticketone.it/events/classica-opera-56/",
        "https://www.ticketone.it/events/jazz-blues-57/",
    ]

    CATEGORY_HINTS = {
        "concerti-55": "concerti",
        "pop-rock-84": "concerti",
        "festival-87": "concerti",
        "hip-hop-rap-98": "concerti",
        "classica-opera-56": "classica",
        "jazz-blues-57": "concerti",
    }

    # Pattern negli slug URL che identificano eventi non-concerto.
    # Filtriamo PRIMA di aprire il browser: risparmia tempo e riduce rumore.
    NOISE_URL_PATTERNS = [
        # Tessere fidelity e abbonamenti calcio.
        # Il pattern "--\d" cattura tutti gli slug TicketOne senza nome venue
        # (doppio trattino finale = placeholder vuoto, es. ssc-napoli--12341442).
        # Sono invariabilmente tessere fidelity, abbonamenti squadre o placeholder.
        "calcio--",
        "fidelity",
        "membership",
        "--",   # slug senza venue: /event/nome-squadra--NNNNNNN/
        # Musei, mostre, visite guidate
        "visita-guidata",
        "visite-guidate",
        "guided-tour",
        "grotte-di-castellana",
        "galleria-colonna",
        "museo-ferragamo",
        "museo-archeologico",
        "cinecitta-si-mostra",
        "chiharu-shiota",
        "liberty-larte",
        "palazzo-martinengo-cesaresco",
        "palazzo-colonna",
        "jack-vettriano",
        "galleria-borghese",
        # Sport motoristici
        "campionato-italiano-velocita",
        "superbike-world-championship",
        "gran-premio-ditalia",
        "mugello-gran-premio",
        "autodromo-internazionale-del-mugello",
        # Sport vari non-concerto
        "davis-cup-finals",
        "cev-eurovolley",
        "internazionali-bnl",
        "bnl-italy-major-premier-padel",
        "nitto-atp-finals",
        "six-nations",
        "amichevoli-nazionali-pallavolo",
        "grand-prix-zeus",
        "iws-the-american-wrestling",
        "iws-showdown",
        "partita-del-cuore",
        "fim-superbike",
        # Attrazioni / parchi / monumenti generici (biglietti ingresso, non concerti)
        "reggia-di-caserta-reggia-di-caserta",
        "parco-archeologico-neapolis",
        "palazzo-velli",
        "mao-museo",
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

    def _get_category_hint(self, url: str) -> str:
        for slug, hint in self.CATEGORY_HINTS.items():
            if slug in url:
                return hint
        return "concerti"

    def _build_page_urls(self, pages: int) -> list[tuple[str, str]]:
        urls = []
        for base_url in self.START_URLS:
            hint = self._get_category_hint(base_url)
            urls.append((base_url, hint))
            for page in range(2, pages + 1):
                urls.append((f"{base_url}?page={page}", hint))
        return urls

    def _is_noise_event(self, item) -> bool:
        """
        Scarta eventi non-concerto identificabili dallo slug URL
        PRIMA di aprire il browser.

        Questi eventi entrano dalla discovery ma non ci interessano
        e non avranno mai una location valida per noi.
        Filtrare qui risparmia un'apertura Chromium per ciascuno.
        """
        url_low = (item.event_url or "").lower()
        return any(pattern in url_low for pattern in self.NOISE_URL_PATTERNS)

    def _is_extra_event(self, item) -> bool:
        """
        Scarta eventi non-concerto: package, VIP, premium, parcheggi ecc.
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
                self.style.WARNING(f"[DISCOVERY SLEEP] {pause:.2f}s")
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
                f"[DISCOVERY URLS] {len(page_urls)} pagine da controllare "
                f"({len(self.START_URLS)} categorie x {pages} pagina/e)"
            )
        )

        for idx, (page_url, category_hint) in enumerate(page_urls, start=1):
            if limit and len(discovered) >= limit:
                break

            self.stdout.write(
                self.style.WARNING(
                    f"[DISCOVERY PAGE] {idx}/{len(page_urls)} "
                    f"category={category_hint} url={page_url}"
                )
            )

            try:
                html = client.get_html(page_url)
                items = parse_event_links(
                    html=html,
                    base_url=page_url,
                    category_hint=category_hint,
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
            self.stdout.write(self.style.WARNING("[STOP] nessun evento trovato"))
            return

        # ----------------------------------------------------------------
        # Filtro pre-browser: scarta URL che sappiamo essere rumore.
        # Applicato PRIMA di enrich_events per non aprire Chromium inutilmente.
        # ----------------------------------------------------------------
        pre_noise_count = 0
        filtered_for_browser = []
        for item in discovered:
            if self._is_noise_event(item):
                pre_noise_count += 1
                if verbose:
                    self.stdout.write(
                        self.style.WARNING(
                            f"[SKIP NOISE PRE-BROWSER] title={item.title} "
                            f"external_id={item.external_id}"
                        )
                    )
            else:
                filtered_for_browser.append(item)

        if pre_noise_count:
            self.stdout.write(
                self.style.WARNING(
                    f"[PRE-BROWSER FILTER] scartati {pre_noise_count} eventi rumore, "
                    f"restano {len(filtered_for_browser)} da aprire con browser"
                )
            )

        results = scraper.enrich_events(filtered_for_browser)

        self.stdout.write(
            self.style.SUCCESS(f"[RESULTS] {len(results)} eventi processati")
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
                f"skipped_noise={pre_noise_count} "
                f"skipped_extra={skipped_extra} "
                f"skipped_location={skipped_location} "
                f"skipped_blocked={skipped_blocked} "
                f"failed_import={failed_import} "
                f"dry_run={dry_run}"
            )
        )

        if not dry_run:
            self.stdout.write(
                self.style.SUCCESS(f"[DB IMPORTED] {imported} eventi")
            )

        self.stdout.write(self.style.SUCCESS("[DONE]"))