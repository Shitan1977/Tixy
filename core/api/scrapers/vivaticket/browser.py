# api/scrapers/vivaticket/browser.py

from playwright.sync_api import sync_playwright


def fetch_vivaticket_page(url: str, headless: bool = False, wait_ms: int = 5000) -> str:
    """
    Apre una pagina Vivaticket con Playwright e restituisce l'HTML.
    Usiamo Playwright perché requests viene bloccato da Incapsula.
    """

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
        )

        context = browser.new_context(
            viewport={"width": 1366, "height": 768},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
            locale="it-IT",
        )

        page = context.new_page()

        print(f"[OPEN] {url}")
        page.goto(url, wait_until="domcontentloaded", timeout=60000)

        page.wait_for_timeout(wait_ms)

        html = page.content()

        browser.close()

        return html


def fetch_vivaticket_music_page(url: str, headless: bool = False) -> str:
    """
    Funzione compatibile con il comando attuale.
    """

    return fetch_vivaticket_page(
        url=url,
        headless=headless,
        wait_ms=5000,
    )
