"""
fansale_checker.py
Checker disponibilità Fansale via Bright Data Scraping Browser.
Usa Playwright remoto per eseguire JS e rilevare disponibilità biglietti.
"""
import os
import re
import asyncio
from typing import Dict, Any

BRIGHTDATA_WS = os.environ.get(
    "BRIGHTDATA_BROWSER_WS",
    "wss://brd-customer-hl_7a402adb-zone-scraping_browser1:7ncl000jvbmg@brd.superproxy.io:9222"
)

AVAILABLE_SIGNALS = [
    "carica offerte",
    "per caricare tutte le offerte",
    "prezzo fisso",
    "acquista ora",
    "aggiungi al carrello",
]

UNAVAILABLE_SIGNALS = [
    "non sono state trovate offerte",
    "nessuna offerta disponibile",
    "no tickets available",
    "sold out",
    "esaurito",
]

async def _check_async(url: str, verbose: bool = False) -> Dict[str, Any]:
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(BRIGHTDATA_WS)
        page = await browser.new_page()

        try:
            if verbose:
                print(f"[FANSALE BROWSER] Opening {url}")

            await page.goto(url, wait_until="domcontentloaded", timeout=90000)
            await page.wait_for_timeout(8000)
            
            # Aspetta che eventuali redirect finiscano
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=10000)
            except Exception:
                pass
            await page.wait_for_timeout(2000)

            try:
                text = await page.evaluate("document.body.innerText")
            except Exception:
                # Retry dopo eventuale navigazione
                await page.wait_for_timeout(5000)
                text = await page.evaluate("document.body.innerText")
            text_lower = text.lower()

            if verbose:
                print(f"[FANSALE BROWSER] Got {len(text)} chars")

            # Controlla prima segnali NON disponibilità (priorità alta)
            for signal in UNAVAILABLE_SIGNALS:
                if signal in text_lower:
                    if verbose:
                        print(f"[FANSALE] Unavailable: '{signal}'")
                    return {
                        "ok": True,
                        "availability": "unavailable",
                        "reason": f"fansale_browser:{signal}",
                        "min_price": None,
                        "url": url,
                    }

            # Poi controlla segnali disponibilità
            for signal in AVAILABLE_SIGNALS:
                if signal in text_lower:
                    if verbose:
                        print(f"[FANSALE] Available: '{signal}'")
                    return {
                        "ok": True,
                        "availability": "available",
                        "reason": f"fansale_browser:{signal}",
                        "min_price": None,
                        "url": url,
                    }

            if verbose:
                print("[FANSALE] Unknown")
            return {
                "ok": True,
                "availability": "unknown",
                "reason": "fansale_browser_no_signal",
                "min_price": None,
                "url": url,
            }

        except Exception as e:
            if verbose:
                print(f"[FANSALE ERROR] {e}")
            return {
                "ok": False,
                "availability": "unknown",
                "reason": f"fansale_error:{e}",
                "min_price": None,
                "url": url,
            }
        finally:
            await browser.close()


def check_fansale_availability(url: str, verbose: bool = False) -> Dict[str, Any]:
    """Wrapper sincrono per il checker asincrono."""
    try:
        return asyncio.run(_check_async(url=url, verbose=verbose))
    except Exception as e:
        return {
            "ok": False,
            "availability": "unknown",
            "reason": f"fansale_asyncio_error:{e}",
            "min_price": None,
            "url": url,
        }
