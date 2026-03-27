from __future__ import annotations

import os
import re
import time
from decimal import Decimal, InvalidOperation
from typing import Optional, Dict

from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.firefox.service import Service
from selenium.common.exceptions import WebDriverException


# =========================================================
# Selenium helpers
# =========================================================

def get_firefox_binary() -> str:
    paths = [
        "/snap/firefox/current/usr/lib/firefox/firefox",
        "/usr/bin/firefox",
    ]
    for p in paths:
        if os.path.exists(p):
            return p
    raise RuntimeError("Firefox non trovato")


def build_driver():
    options = Options()
    options.add_argument("--headless")
    options.binary_location = get_firefox_binary()

    options.set_preference(
        "general.useragent.override",
        "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"
    )
    options.set_preference("dom.webdriver.enabled", False)
    options.set_preference("useAutomationExtension", False)
    options.set_preference("media.peerconnection.enabled", False)

    service = Service()

    driver = webdriver.Firefox(
        service=service,
        options=options,
    )
    driver.set_window_size(1600, 1200)

    try:
        driver.execute_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
    except Exception:
        pass

    return driver


def fetch_html_with_driver(driver, url: str, wait_seconds: int = 10) -> str:
    driver.get(url)
    time.sleep(4)

    try:
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(2)
        driver.execute_script("window.scrollTo(0, 0);")
        time.sleep(1)
    except Exception:
        pass

    end_time = time.time() + wait_seconds
    last_html = ""

    while time.time() < end_time:
        html = driver.page_source
        last_html = html

        page_text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
        if "€" in page_text or "eur" in page_text.lower() or "offerte da" in page_text.lower():
            return html

        time.sleep(1)

    return last_html


# =========================================================
# Price parsing helpers
# =========================================================

def parse_price_text_to_decimal(price_text: str) -> Optional[Decimal]:
    """
    Converte testi tipo:
    - '€ 95,00'
    - '95,00 €'
    - 'EUR 95,00'
    - 'da EUR 95,50'
    in Decimal('95.00')
    """
    if not price_text:
        return None

    txt = price_text.strip().lower()
    txt = txt.replace("\xa0", " ")
    txt = re.sub(r"\s+", " ", txt)

    # prende il primo numero con formato europeo
    match = re.search(r"(\d{1,3}(?:\.\d{3})*,\d{2}|\d+,\d{2}|\d+)", txt)
    if not match:
        return None

    raw = match.group(1)
    raw = raw.replace(".", "").replace(",", ".")

    try:
        return Decimal(raw)
    except (InvalidOperation, ValueError):
        return None


def extract_price_candidates_from_text(text: str) -> list[str]:
    candidates = []

    patterns = [
        r"(?:€|eur)\s*\d{1,3}(?:\.\d{3})*,\d{2}",
        r"\d{1,3}(?:\.\d{3})*,\d{2}\s*(?:€|eur)",
        r"(?:da\s+)?(?:€|eur)\s*\d+(?:,\d{2})?",
        r"(?:da\s+)?\d+(?:,\d{2})?\s*(?:€|eur)",
    ]

    for pattern in patterns:
        found = re.findall(pattern, text, flags=re.IGNORECASE)
        candidates.extend(found)

    # deduplica mantenendo ordine
    seen = set()
    ordered = []
    for c in candidates:
        norm = c.strip().lower()
        if norm not in seen:
            seen.add(norm)
            ordered.append(c.strip())

    return ordered


def extract_offer_price_from_html(html: str) -> Dict[str, Optional[str]]:
    soup = BeautifulSoup(html, "html.parser")

    # testo completo pagina
    full_text = soup.get_text(" ", strip=True)
    full_text = re.sub(r"\s+", " ", full_text)

    candidates = extract_price_candidates_from_text(full_text)

    price_text = candidates[0] if candidates else None
    price_value = parse_price_text_to_decimal(price_text) if price_text else None

    currency = None
    if price_text:
        if "€" in price_text or "eur" in price_text.lower():
            currency = "EUR"

    return {
        "price_text": price_text,
        "price_value": str(price_value) if price_value is not None else None,
        "currency": currency,
    }


# =========================================================
# Main test function
# =========================================================

def test_offer_price(url: str):
    driver = None
    try:
        driver = build_driver()
        html = fetch_html_with_driver(driver, url, wait_seconds=10)

        result = extract_offer_price_from_html(html)

        print("\n========== RESULT ==========")
        print("URL       :", url)
        print("TITLE     :", driver.title)
        print("PRICE TXT :", result["price_text"])
        print("PRICE NUM :", result["price_value"])
        print("CURRENCY  :", result["currency"])
        print("============================\n")

        return result

    except WebDriverException as e:
        print("[SELENIUM ERROR]", str(e))
        return None

    except Exception as e:
        print("[ERROR]", str(e))
        return None

    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass


if __name__ == "__main__":
    TEST_URL = "https://www.fansale.it/tickets/all/giorgia/464800/12345678"
    test_offer_price(TEST_URL)