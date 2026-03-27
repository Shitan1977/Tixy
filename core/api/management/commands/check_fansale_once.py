from django.core.management.base import BaseCommand
from django.utils import timezone
import requests


class Command(BaseCommand):
    help = "Debug singola URL fanSALE"

    def add_arguments(self, parser):
        parser.add_argument("--url", type=str, required=True)

    def handle(self, *args, **options):
        url = options["url"]

        self.stdout.write(f"[CHECK] {url}")

        session = requests.Session()
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        })

        try:
            r = session.get(url, timeout=20, allow_redirects=True)

            html = (r.text or "").lower()

            self.stdout.write(f"[STATUS] {r.status_code}")
            self.stdout.write(f"[FINAL URL] {r.url}")
            self.stdout.write(f"[LEN HTML] {len(html)}")

            # 🔍 check segnali
            if "challenge page" in html:
                self.stdout.write("[RESULT] challenge_page 🚫")
                return

            if "access denied" in html:
                self.stdout.write("[RESULT] access_denied 🚫")
                return

            if "biglietti" in html or "tickets" in html:
                self.stdout.write("[RESULT] POSSIBILE DISPONIBILITÀ 🎟️")
                return

            self.stdout.write("[RESULT] unknown 🤷")

        except requests.exceptions.ReadTimeout:
            self.stdout.write("[RESULT] read_timeout ⏱️")

        except Exception as e:
            self.stdout.write(f"[ERROR] {type(e).__name__}: {e}")