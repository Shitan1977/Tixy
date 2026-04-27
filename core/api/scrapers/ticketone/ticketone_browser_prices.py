from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth
import time
import random
import os


class TicketOneBrowserPrices:
    def __init__(self, headless=False, verbose=False):
        self.headless = headless
        self.verbose = verbose

        self.stealth = Stealth()

        self.playwright_cm = None
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None

    def start(self):
        self.playwright_cm = self.stealth.use_sync(sync_playwright())
        self.playwright = self.playwright_cm.__enter__()

        self.browser = self.playwright.chromium.launch(
            headless=self.headless,
            args=[
                "--disable-blink-features=AutomationControlled",
            ]
        )

        self.context = self.browser.new_context(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            locale="it-IT",
            viewport={"width": 1366, "height": 768},
            timezone_id="Europe/Rome",
        )

        self.page = self.context.new_page()

    def stop(self):
        if self.page:
            try:
                self.page.close()
            except Exception:
                pass

        if self.context:
            try:
                self.context.close()
            except Exception:
                pass

        if self.browser:
            try:
                self.browser.close()
            except Exception:
                pass

        if self.playwright_cm:
            try:
                self.playwright_cm.__exit__(None, None, None)
            except Exception:
                pass

    def _sleep(self, min_s=2.5, max_s=5.5):
        s = random.uniform(min_s, max_s)
        if self.verbose:
            print(f"[BROWSER SLEEP] {s:.2f}s")
        time.sleep(s)

    def _dump_debug_artifacts(self, prefix: str = "/tmp/ticketone_prices_debug"):
        """
        Salva HTML, screenshot, titolo e URL finale per capire cosa sta vedendo davvero il browser.
        """
        if not self.page:
            return

        base_dir = os.path.dirname(prefix)
        if base_dir:
            os.makedirs(base_dir, exist_ok=True)

        try:
            html = self.page.content()
        except Exception:
            html = ""

        try:
            title = self.page.title()
        except Exception:
            title = ""

        try:
            current_url = self.page.url
        except Exception:
            current_url = ""

        try:
            with open(f"{prefix}.html", "w", encoding="utf-8") as f:
                f.write(html)
        except Exception as e:
            if self.verbose:
                print(f"[DEBUG SAVE HTML ERROR] {e}")

        try:
            with open(f"{prefix}.txt", "w", encoding="utf-8") as f:
                f.write(f"TITLE: {title}\n")
                f.write(f"URL: {current_url}\n")
        except Exception as e:
            if self.verbose:
                print(f"[DEBUG SAVE TXT ERROR] {e}")

        try:
            self.page.screenshot(path=f"{prefix}.png", full_page=True)
        except Exception as e:
            if self.verbose:
                print(f"[DEBUG SAVE PNG ERROR] {e}")

        if self.verbose:
            print(f"[DEBUG SAVED] {prefix}.html {prefix}.txt {prefix}.png")

    def _looks_blocked(self, html: str, title: str) -> bool:
        title_low = (title or "").lower()
        html_low = (html or "").lower()

        blocked_markers = [
            "access denied",
            "forbidden",
            "captcha",
            "verify you are human",
            "bot",
            "temporarily unavailable",
        ]

        if any(marker in html_low for marker in blocked_markers):
            return True

        if any(marker in title_low for marker in blocked_markers):
            return True

        return False

    def get_html(self, url: str, save_debug_prefix: str = "/tmp/ticketone_prices_debug") -> str:
        if self.verbose:
            print(f"[BROWSER OPEN] {url}")

        max_retries = 2
        last_title = ""
        last_url = ""

        for attempt in range(max_retries):
            try:
                time.sleep(random.uniform(2, 4))

                self.page.goto(
                    "https://www.ticketone.it/",
                    timeout=60000,
                    wait_until="domcontentloaded"
                )
                self._sleep(4, 7)

                for _ in range(random.randint(2, 4)):
                    self.page.mouse.wheel(0, random.randint(400, 1200))
                    self._sleep(0.8, 1.5)

                self.page.goto(
                    "https://www.ticketone.it/events/concerti-55/",
                    timeout=60000,
                    wait_until="domcontentloaded"
                )
                self._sleep(3, 5)

                for _ in range(random.randint(2, 3)):
                    self.page.mouse.wheel(0, random.randint(500, 1200))
                    self._sleep(1, 2)

                self.page.goto(
                    url,
                    timeout=60000,
                    referer="https://www.ticketone.it/events/concerti-55/",
                    wait_until="domcontentloaded"
                )

                self._sleep(4, 6)

                for _ in range(random.randint(3, 6)):
                    self.page.mouse.wheel(0, random.randint(200, 900))
                    self._sleep(0.8, 1.8)

                html = self.page.content()
                last_title = self.page.title()
                last_url = self.page.url

                if self.verbose:
                    print(f"[PAGE TITLE] {last_title}")
                    print(f"[PAGE URL] {last_url}")
                    print(f"[HTML LENGTH] {len(html)}")

                if self._looks_blocked(html, last_title):
                    self._dump_debug_artifacts(prefix=f"{save_debug_prefix}_attempt{attempt + 1}")
                    if self.verbose:
                        print(f"[BLOCKED] Tentativo {attempt + 1}")
                    self._sleep(5, 9)
                    continue

                self._dump_debug_artifacts(prefix=f"{save_debug_prefix}_success")
                return html

            except Exception as e:
                if self.verbose:
                    print(f"[RETRY] attempt={attempt + 1} error={e}")
                try:
                    self._dump_debug_artifacts(prefix=f"{save_debug_prefix}_error_attempt{attempt + 1}")
                except Exception:
                    pass
                self._sleep(5, 9)

        raise RuntimeError(f"Blocked after retries | last_title={last_title!r} | last_url={last_url!r}")