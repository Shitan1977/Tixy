import os
import random
import re
import time
from urllib.parse import urlparse, urlunparse

from django.core.management.base import BaseCommand

from api.scrapers.ticketone.browser import TicketOneBrowser
from api.scrapers.ticketone.importer import import_ticketone_item
from api.scrapers.ticketone.parser import parse_event_links
from api.scrapers.ticketone.runner import TicketOneScraper

# Path default del file artisti, relativo alla root del progetto Django.
# Può essere sovrascritto con --file.
DEFAULT_ARTIST_FILE = os.path.join(
    os.path.dirname(__file__),           # .../management/commands/
    "..", "..",                           # .../api/
    "scrapers", "ticketone",
    "artist_urls.txt",
)

# Stessi pattern di scrub_ticketone_full — copiati qui per non creare
# dipendenze tra i due command. Se li aggiorni lì, aggiornali anche qui.
NOISE_URL_PATTERNS = [
    "calcio--", "fidelity", "membership", "--",
    "visita-guidata", "visite-guidate", "guided-tour",
    "grotte-di-castellana", "galleria-colonna", "museo-ferragamo",
    "museo-archeologico", "cinecitta-si-mostra", "chiharu-shiota",
    "liberty-larte", "palazzo-martinengo-cesaresco", "palazzo-colonna",
    "jack-vettriano", "galleria-borghese",
    "campionato-italiano-velocita", "superbike-world-championship",
    "gran-premio-ditalia", "mugello-gran-premio",
    "autodromo-internazionale-del-mugello",
    "davis-cup-finals", "cev-eurovolley", "internazionali-bnl",
    "bnl-italy-major-premier-padel", "nitto-atp-finals", "six-nations",
    "amichevoli-nazionali-pallavolo", "grand-prix-zeus",
    "iws-the-american-wrestling", "iws-showdown", "partita-del-cuore",
    "fim-superbike",
    "reggia-di-caserta-reggia-di-caserta",
    "parco-archeologico-neapolis", "palazzo-velli", "mao-museo",
]

NOISE_TITLE_PATTERNS = [
    "fidelity", "membership card", "season ticket",
    "abbonamento reggia", "visita guidata", "visite guidate", "guided tour",
]

SKIP_KEYWORDS = [
    "package", "vip", "premium", "party terrace",
    "parcheggio", "parking", "reservation",
    "abbonamento", "full pass", "august pass",
]

EMPTY_SLUG_RE = re.compile(r"/event/[^/]+--\d{5,}/")


def _clean_artist_url(url: str) -> str:
    """
    Rimuove parametri query dall'URL artista (?affiliate=IGA ecc.).
    TicketOne non richiede parametri per le pagine artista.
    """
    parsed = urlparse(url.strip())
    return urlunparse(parsed._replace(query="", fragment=""))


def _load_artist_urls(filepath: str) -> list[str]:
    """
    Carica URL artista dal file seed.
    Ignora righe vuote e commenti (#).
    Rimuove duplicati preservando l'ordine.
    """
    if not os.path.exists(filepath):
        return []

    seen = set()
    urls = []

    with open(filepath, encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            url = _clean_artist_url(line)
            if url and url not in seen:
                seen.add(url)
                urls.append(url)

    return urls


def _is_noise(url: str, title: str) -> tuple[bool, str]:
    """
    Restituisce (True, motivo) se l'evento è rumore, (False, "") altrimenti.
    Stessa logica di scrub_ticketone_full._is_noise_event.
    """
    url_low = url.lower()
    title_low = title.lower()

    for pattern in NOISE_URL_PATTERNS:
        if pattern in url_low:
            return True, f"url_pattern='{pattern}'"

    for pattern in NOISE_TITLE_PATTERNS:
        if pattern in title_low:
            return True, f"title_pattern='{pattern}'"

    if EMPTY_SLUG_RE.search(url):
        return True, "empty_slug"

    return False, ""


def _is_extra(url: str, title: str) -> bool:
    text = f"{title.lower()} {url.lower()}"
    return any(kw in text for kw in SKIP_KEYWORDS)


def _has_valid_location(item) -> bool:
    return bool(item.city and item.venue)


class Command(BaseCommand):
    help = (
        "Scansiona pagine artista TicketOne, estrae tutti gli eventi "
        "e li importa nel DB. Richiede xvfb-run (browser headless)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--file",
            type=str,
            default=os.path.normpath(DEFAULT_ARTIST_FILE),
            help="Path al file con gli URL artista (default: artist_urls.txt)"
        )
        parser.add_argument(
            "--artist-url",
            type=str,
            default=None,
            help="URL artista singolo da scansionare (ignora --file)"
        )
        parser.add_argument("--limit", type=int, default=200)
        parser.add_argument("--verbose", action="store_true")
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument(
            "--sleep-min", type=float, default=5.0,
            help="Pausa minima tra aperture browser (secondi)"
        )
        parser.add_argument(
            "--sleep-max", type=float, default=10.0,
            help="Pausa massima tra aperture browser (secondi)"
        )

    def _log(self, msg: str, style=None):
        if style:
            self.stdout.write(style(msg))
        else:
            self.stdout.write(msg)

    def _discover_from_artist_page(
        self, artist_url: str, verbose: bool
    ) -> list:
        """
        Apre la pagina artista con Playwright e ne estrae i link evento.
        Restituisce lista di TicketOneEventItem.
        """
        browser = TicketOneBrowser(headless=False, verbose=verbose)
        try:
            browser.start()
            html = browser.get_html(artist_url)
            items = parse_event_links(
                html=html,
                base_url=artist_url,
                category_hint="concerti",
            )
            return items
        except Exception as exc:
            self._log(
                f"[ARTIST ERROR] url={artist_url} error={exc}",
                self.style.ERROR
            )
            return []
        finally:
            try:
                browser.stop()
            except Exception:
                pass

    def handle(self, *args, **options):
        verbose = options["verbose"]
        dry_run = options["dry_run"]
        limit = options["limit"]
        sleep_min = options["sleep_min"]
        sleep_max = options["sleep_max"]
        artist_file = options["file"]
        single_url = options["artist_url"]

        self._log(
            f"[START] scrub_ticketone_artists "
            f"limit={limit} dry_run={dry_run}",
            self.style.WARNING
        )

        # --- Carica lista artisti ---
        if single_url:
            artist_urls = [_clean_artist_url(single_url)]
            self._log(
                f"[MODE] URL singolo: {artist_urls[0]}",
                self.style.WARNING
            )
        else:
            artist_urls = _load_artist_urls(artist_file)
            self._log(
                f"[MODE] File: {artist_file} → {len(artist_urls)} artisti",
                self.style.WARNING
            )

        if not artist_urls:
            self._log("[STOP] Nessun URL artista trovato.", self.style.ERROR)
            return

        # --- Discovery: una pagina artista alla volta con browser ---
        all_items = []
        seen_urls = set()

        for idx, artist_url in enumerate(artist_urls, start=1):
            self._log(
                f"[ARTIST] {idx}/{len(artist_urls)} {artist_url}",
                self.style.WARNING
            )

            items = self._discover_from_artist_page(artist_url, verbose)

            new_count = 0
            for item in items:
                if item.event_url in seen_urls:
                    continue
                seen_urls.add(item.event_url)
                all_items.append(item)
                new_count += 1

            self._log(
                f"[ARTIST FOUND] {artist_url} → {len(items)} link, "
                f"{new_count} nuovi (totale={len(all_items)})",
                self.style.SUCCESS
            )

            if limit and len(all_items) >= limit:
                all_items = all_items[:limit]
                self._log(
                    f"[LIMIT] raggiunto limit={limit}, interrompo discovery",
                    self.style.WARNING
                )
                break

            if idx < len(artist_urls):
                pause = random.uniform(sleep_min, sleep_max)
                if verbose:
                    self._log(f"[SLEEP] {pause:.2f}s tra artisti")
                time.sleep(pause)

        self._log(
            f"[DISCOVERED TOTAL] {len(all_items)} eventi unici da pagine artista",
            self.style.SUCCESS
        )

        if not all_items:
            self._log("[STOP] Nessun evento trovato.", self.style.WARNING)
            return

        # --- Filtro pre-browser: scarta rumore senza aprire Chromium ---
        noise_count = 0
        filtered = []

        for item in all_items:
            is_noise, reason = _is_noise(item.event_url, item.title)
            if is_noise:
                noise_count += 1
                if verbose:
                    self._log(
                        f"[SKIP NOISE PRE-BROWSER] reason={reason} "
                        f"title={item.title} external_id={item.external_id}",
                        self.style.WARNING
                    )
            else:
                filtered.append(item)

        if noise_count:
            self._log(
                f"[PRE-BROWSER FILTER] scartati {noise_count} rumore, "
                f"restano {len(filtered)} da arricchire",
                self.style.WARNING
            )

        # --- Enrich: apre ogni pagina dettaglio con browser ---
        scraper = TicketOneScraper(verbose=verbose)
        results = scraper.enrich_events(filtered)

        ok_count = sum(1 for i in results if i.detail_status == "ok")
        blocked_count = sum(1 for i in results if i.detail_status == "blocked")

        self._log(f"[RESULTS] {len(results)} processati", self.style.SUCCESS)
        self._log(f"[DETAIL OK] {ok_count}", self.style.SUCCESS)
        self._log(f"[DETAIL BLOCKED] {blocked_count}", self.style.WARNING)

        # --- Import ---
        imported = 0
        skipped_extra = 0
        skipped_location = 0
        skipped_blocked = 0
        failed_import = 0

        for item in results:
            self._log(
                f"- title={item.title} | city={item.city} | venue={item.venue} | "
                f"date={item.starts_at_raw} | price={item.price_text} | "
                f"external_id={item.external_id} | detail_status={item.detail_status} | "
                f"url={item.event_url}"
            )

            if item.detail_status != "ok":
                skipped_blocked += 1
                self._log(
                    f"[SKIP BLOCKED] title={item.title}",
                    self.style.WARNING
                )
                continue

            if _is_extra(item.event_url, item.title):
                skipped_extra += 1
                self._log(
                    f"[SKIP EXTRA] title={item.title} external_id={item.external_id}",
                    self.style.WARNING
                )
                continue

            if not _has_valid_location(item):
                skipped_location += 1
                self._log(
                    f"[SKIP LOCATION MISSING] title={item.title} "
                    f"city={item.city} venue={item.venue} "
                    f"external_id={item.external_id}",
                    self.style.WARNING
                )
                continue

            if dry_run:
                self._log(
                    f"[DRY RUN OK] importabile title={item.title} "
                    f"external_id={item.external_id}",
                    self.style.SUCCESS
                )
                continue

            try:
                outcome = import_ticketone_item(item)
                imported += 1
                if verbose:
                    self._log(
                        f"[IMPORTED] evento_id={outcome['evento_id']} "
                        f"performance_id={outcome['performance_id']}",
                        self.style.SUCCESS
                    )
            except Exception as exc:
                failed_import += 1
                self._log(
                    f"[IMPORT ERROR] title={item.title} "
                    f"external_id={item.external_id} error={exc}",
                    self.style.ERROR
                )

        self._log(
            f"[SUMMARY] imported={imported} "
            f"skipped_noise={noise_count} "
            f"skipped_extra={skipped_extra} "
            f"skipped_location={skipped_location} "
            f"skipped_blocked={skipped_blocked} "
            f"failed_import={failed_import} "
            f"dry_run={dry_run}",
            self.style.WARNING
        )

        if not dry_run:
            self._log(f"[DB IMPORTED] {imported} eventi", self.style.SUCCESS)

        self._log("[DONE]", self.style.SUCCESS)