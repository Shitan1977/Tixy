from __future__ import annotations

import time

from django.core.management.base import BaseCommand

from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.firefox.options import Options


class Command(BaseCommand):
    help = "Debug manuale fanSALE con Selenium + Firefox headless"

    def add_arguments(self, parser):
        parser.add_argument("--url", type=str, required=True, help="URL fanSALE da aprire")
        parser.add_argument("--wait", type=int, default=8, help="Secondi di attesa dopo il caricamento")
        parser.add_argument(
            "--binary",
            type=str,
            default="/snap/firefox/current/usr/lib/firefox/firefox",
            help="Percorso binario Firefox reale",
        )

    def handle(self, *args, **options):
        url = options["url"]
        wait_s = int(options["wait"])
        binary = options["binary"]

        self.stdout.write(f"[START] url={url}")
        self.stdout.write(f"[BINARY] {binary}")

        driver = None

        try:
            firefox_options = Options()
            firefox_options.binary_location = binary
            firefox_options.add_argument("--headless")

            driver = webdriver.Firefox(options=firefox_options)
            driver.set_page_load_timeout(30)

            self.stdout.write("[OPEN] apro la pagina...")
            driver.get(url)

            if wait_s > 0:
                self.stdout.write(f"[WAIT] attendo {wait_s} secondi...")
                time.sleep(wait_s)

            title = driver.title or ""
            current_url = driver.current_url or ""
            page_source = driver.page_source or ""
            html_norm = page_source.lower()

            self.stdout.write(f"[TITLE] {title}")
            self.stdout.write(f"[FINAL URL] {current_url}")
            self.stdout.write(f"[HTML LEN] {len(page_source)}")

            if "challenge page" in html_norm:
                self.stdout.write("[RESULT] challenge_page")
            elif "access denied" in html_norm:
                self.stdout.write("[RESULT] access_denied")
            elif "captcha" in html_norm:
                self.stdout.write("[RESULT] captcha")
            elif "biglietti" in html_norm or "tickets" in html_norm:
                self.stdout.write("[RESULT] possible_ticket_content")
            else:
                self.stdout.write("[RESULT] unknown")

            preview = page_source[:1200].replace("\n", " ").replace("\r", " ")
            self.stdout.write(f"[PREVIEW] {preview}")

        except TimeoutException as e:
            self.stdout.write(f"[TIMEOUT] {e}")

        except WebDriverException as e:
            self.stdout.write(f"[WEBDRIVER ERROR] {e}")

        except Exception as e:
            self.stdout.write(f"[ERROR] {type(e).__name__}: {e}")

        finally:
            if driver:
                try:
                    driver.quit()
                except Exception:
                    pass

            self.stdout.write("[DONE]")