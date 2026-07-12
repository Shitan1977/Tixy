import re
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from io import BytesIO
from typing import Any, Dict, List

import requests
from celery import shared_task
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import transaction
from django.utils import timezone

from .models import Biglietto, Performance, TicketUpload

MAX_TICKET_PDF_BYTES = 15 * 1024 * 1024

# --- helper sicuri: nessuna dipendenza hard obbligatoria ---
try:
    from pypdf import PdfReader
except Exception:
    PdfReader = None

try:
    from pdfminer.high_level import extract_text as pdfminer_extract_text
except Exception:
    pdfminer_extract_text = None

try:
    from pdf2image import convert_from_bytes
    from PIL import Image
except Exception:
    convert_from_bytes = None
    Image = None

try:
    from pyzbar.pyzbar import decode as zbar_decode
except Exception:
    zbar_decode = None


def _read_all_bytes(biglietto: Biglietto) -> bytes:
    if not biglietto.path_file:
        raise RuntimeError("File PDF non presente")
    with default_storage.open(biglietto.path_file.name, "rb") as f:
        return f.read()


def _download_source_pdf(url: str) -> bytes:
    """
    Scarica il PDF di un e-ticket dall'URL fornito dall'utente.
    Limiti: max 15MB, timeout 30s, il contenuto deve essere un PDF reale.
    """
    url = (url or "").strip()
    if not url:
        raise RuntimeError("File PDF non presente")

    try:
        resp = requests.get(
            url,
            timeout=30,
            stream=True,
            allow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; TixyTicketBot/1.0)"},
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        raise RuntimeError(f"impossibile scaricare l'e-ticket dall'URL: {e}") from e

    chunks = []
    total = 0
    for chunk in resp.iter_content(64 * 1024):
        total += len(chunk)
        if total > MAX_TICKET_PDF_BYTES:
            raise RuntimeError("e-ticket troppo grande (max 15MB)")
        chunks.append(chunk)
    data = b"".join(chunks)

    # alcuni generatori antepongono junk/BOM all'header PDF
    if b"%PDF-" not in data[:1024]:
        raise RuntimeError("l'URL non restituisce un PDF: apri il link, scarica il biglietto e caricalo come file")
    return data


def _safe_sha256(b: bytes) -> str:
    return sha256(b).hexdigest()


def _pdf_pages_count(pdf_bytes: bytes) -> int:
    if not PdfReader:
        return None
    try:
        reader = PdfReader(BytesIO(pdf_bytes))
        return len(reader.pages)
    except Exception:
        return None


def _normalize_spaces(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "")).strip()


def _normalize_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").lower())


def _parse_decimal(raw: str):
    if not raw:
        return None
    try:
        return Decimal(raw.replace(".", "").replace(",", ".")).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _extract_text(pdf_bytes: bytes) -> str:
    texts = []

    if pdfminer_extract_text:
        try:
            text = pdfminer_extract_text(BytesIO(pdf_bytes)) or ""
            if text.strip():
                texts.append(text)
        except Exception:
            pass

    if PdfReader:
        try:
            reader = PdfReader(BytesIO(pdf_bytes))
            page_texts = []
            for page in reader.pages:
                try:
                    page_text = page.extract_text() or ""
                except Exception:
                    page_text = ""
                if page_text.strip():
                    page_texts.append(page_text)
            if page_texts:
                texts.append("\n".join(page_texts))
        except Exception:
            pass

    combined = "\n".join(t for t in texts if t).strip()
    return combined


def _extract_ticket_sections(text: str) -> List[str]:
    cleaned = (text or "").strip()
    if not cleaned:
        return []
    parts = re.split(r"(?=Il tuo biglietto\s*DATI ORDINE)", cleaned, flags=re.I)
    sections = [_normalize_spaces(part) for part in parts if _normalize_spaces(part)]
    return sections or [_normalize_spaces(cleaned)]


def _extract_names(text: str) -> List[str]:
    names = []
    patterns = [
        r"(?:Intestatario|Nome|Nominativo|Holder)\s*[:\-]\s*([A-ZÀ-Ý][^\n]+)",
        r"(?:PIT|TRIBUNA|POSTO|INTERO|RIDOTTO|PLATEA){1,4}\s*([A-Za-zÀ-ÿ'\s]{5,80}?)\s*Prezzo\s*€",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, text, re.I):
            candidate = _normalize_spaces(match.group(1))
            if candidate and candidate.lower() not in {name.lower() for name in names}:
                names.append(candidate[:120])
    return names[:20]


def _extract_prices(text: str) -> List[Decimal]:
    prices = []
    seen = set()
    patterns = [
        r"Prezzo\s*€?\s*:?\s*(\d{1,4},\d{2})",
        r"Totale\s*€?\s*:?\s*(\d{1,4},\d{2})",
        r"€\s*(\d{1,4},\d{2})",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, text, re.I):
            price = _parse_decimal(match.group(1))
            if price is not None and price not in seen:
                prices.append(price)
                seen.add(price)
    return prices[:20]


def _parse_event_datetime(text: str):
    month_map = {
        "gennaio": 1,
        "febbraio": 2,
        "marzo": 3,
        "aprile": 4,
        "maggio": 5,
        "giugno": 6,
        "luglio": 7,
        "agosto": 8,
        "settembre": 9,
        "ottobre": 10,
        "novembre": 11,
        "dicembre": 12,
    }
    # formato TicketOne: "Data: 11 Luglio 2026 Ore: 21:00"
    match = re.search(r"Data\s*:\s*(\d{1,2})\s+([A-Za-zÀ-ÿ]+)\s+(\d{4})\s*Ore\s*:\s*(\d{1,2}):(\d{2})", text, re.I)
    if not match:
        # formato Ticketmaster: "venerdì, 17 luglio 2026 - h 20:45"
        match = re.search(r"(\d{1,2})\s+([A-Za-zÀ-ÿ]+)\s+(\d{4})\s*[-–]\s*h\.?\s*(\d{1,2})[:.](\d{2})", text, re.I)
    if not match:
        return None
    day, month_name, year, hour, minute = match.groups()
    month = month_map.get(month_name.strip().lower())
    if not month:
        return None
    try:
        dt = datetime(int(year), month, int(day), int(hour), int(minute), tzinfo=dt_timezone.utc)
    except ValueError:
        return None
    return dt


def _extract_event_meta(text: str) -> Dict[str, Any]:
    event_name = None
    venue = None
    event_dt = _parse_event_datetime(text)

    event_match = re.search(
        r"Sigillo\s+Fiscale\s*:\s*[a-f0-9]{8,32}\s*([A-Za-zÀ-ÿ0-9 .,'&\-/]{2,120}?)\s*Apertura\s+porte",
        text,
        re.I,
    )
    if event_match:
        event_name = _normalize_spaces(event_match.group(1))

    venue_match = re.search(
        r"Apertura\s+porte(?:\s+ore)?\s*\d{1,2}[.:]\d{2}\s*(.*?)\s*Data\s*:",
        text,
        re.I,
    )
    if venue_match:
        venue = _normalize_spaces(venue_match.group(1))

    if not event_name:
        fallback_event = re.search(r"(?:Titolo digitale|TITOLO DIGITALE)\s*([A-Za-zÀ-ÿ0-9 .,'&\-/]{2,120}?)\s*Apertura\s+porte", text, re.I)
        if fallback_event:
            event_name = _normalize_spaces(fallback_event.group(1))

    return {
        "event_name": event_name,
        "venue": venue,
        "event_date": event_dt,
    }


def _extract_ticket_codes(section_text: str) -> Dict[str, Any]:
    sigillo = None
    ticket_id = None
    et_code = None
    barcode = None

    # copre "Sigillo Fiscale:", "S.F.:", "S.F:", "SF:" (Ticketmaster usa "SF:")
    sigillo_match = re.search(r"(?:Sigillo\s+Fiscale|S\.?F\.?)\s*:\s*([a-f0-9]{8,32})", section_text, re.I)
    if sigillo_match:
        sigillo = sigillo_match.group(1).lower()

    ticket_id_match = re.search(r"TktID\s*:\s*(\d{6,20})", section_text, re.I)
    if ticket_id_match:
        ticket_id = ticket_id_match.group(1)

    et_match = re.search(r"\bET\s*:\s*(\d{6,20})", section_text, re.I)
    if et_match:
        et_code = et_match.group(1)

    if not ticket_id:
        tn_match = re.search(r"\bTN\s*:\s*(\d{6,20})", section_text, re.I)
        if tn_match:
            ticket_id = tn_match.group(1)

    # fallback universale: numero di codice a barre "nudo" (sequenza lunga standalone)
    barcode_match = re.search(r"(?<!\w)(\d{16,30})(?!\w)", section_text)
    if barcode_match:
        barcode = barcode_match.group(1)

    return {
        "sigillo": sigillo,
        "ticket_id": ticket_id,
        "et_code": et_code,
        "barcode": barcode,
    }


def _build_ticket_rows(text: str) -> List[Dict[str, Any]]:
    sections = _extract_ticket_sections(text)
    rows = []
    fallback_names = _extract_names(text)
    fallback_prices = _extract_prices(text)

    for index, section in enumerate(sections, start=1):
        code_data = _extract_ticket_codes(section)
        section_names = _extract_names(section)
        section_prices = _extract_prices(section)
        raw_code = (
            code_data["sigillo"]
            or code_data["ticket_id"]
            or code_data["et_code"]
            or code_data.get("barcode")
        )
        if not raw_code:
            continue
        if code_data["sigillo"]:
            code_type = "SIGILLO"
        elif code_data["ticket_id"] or code_data["et_code"]:
            code_type = "TKTID"
        else:
            code_type = "BARCODE"
        rows.append(
            {
                "page": index,
                "code_type": code_type,
                "code_raw": raw_code,
                "sigillo": code_data["sigillo"],
                "ticket_id": code_data["ticket_id"],
                "full_name": (section_names or fallback_names or [None])[0],
                "price": (section_prices or fallback_prices or [None])[0],
            }
        )

    return rows


def _serialize_subitems_for_upload(rows: List[Dict[str, Any]], codes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Produce subitems temporanei per la review senza persistere TicketSubitem prima del confirm.
    """
    temp_rows: List[Dict[str, Any]] = []
    seen_hashes = set()

    for row in rows:
        raw = (row.get("code_raw") or "").strip()
        if not raw:
            continue
        code_hash = _safe_sha256(raw.encode("utf-8"))
        if code_hash in seen_hashes:
            continue
        seen_hashes.add(code_hash)
        temp_rows.append(
            {
                "id": len(temp_rows) + 1,
                "page": row.get("page"),
                "code_type": row.get("code_type") or "CODE",
                "code_raw": raw,
                "code_hash": code_hash,
                "full_name": row.get("full_name"),
                "price": str(row.get("price")) if row.get("price") is not None else None,
            }
        )

    if temp_rows:
        return temp_rows

    for code in codes or []:
        raw = (code.get("code_raw") or "").strip()
        if not raw:
            continue
        code_hash = _safe_sha256(raw.encode("utf-8"))
        if code_hash in seen_hashes:
            continue
        seen_hashes.add(code_hash)
        temp_rows.append(
            {
                "id": len(temp_rows) + 1,
                "page": code.get("page"),
                "code_type": code.get("code_type") or "CODE",
                "code_raw": raw,
                "code_hash": code_hash,
                "full_name": None,
                "price": None,
            }
        )

    return temp_rows


def _find_matching_performance(event_name: str, venue: str, event_dt: datetime):
    if not event_name or not venue or not event_dt:
        return None

    event_key = _normalize_key(event_name)
    venue_key = _normalize_key(venue)
    if not event_key or not venue_key:
        return None

    start = event_dt - timedelta(hours=8)
    end = event_dt + timedelta(hours=8)
    candidates = (
        Performance.objects.select_related("evento", "luogo")
        .filter(starts_at_utc__gte=timezone.now(), starts_at_utc__range=(start, end))
    )

    best = None
    best_score = -1
    for perf in candidates:
        perf_event_key = _normalize_key(getattr(perf.evento, "nome_evento", ""))
        perf_venue_key = _normalize_key(getattr(perf.luogo, "nome", ""))
        score = 0
        if event_key == perf_event_key:
            score += 3
        elif event_key in perf_event_key or perf_event_key in event_key:
            score += 2
        if venue_key == perf_venue_key:
            score += 3
        elif venue_key in perf_venue_key or perf_venue_key in venue_key:
            score += 2
        if perf.starts_at_utc.date() == event_dt.date():
            score += 2
        if score > best_score:
            best = perf
            best_score = score

    return best if best_score >= 5 else None


def _find_performance_in_text(full_text: str, event_dt: datetime):
    """
    Match "invertito" e indipendente dal layout del biglietto: invece di estrarre
    nome evento/luogo con regex legate a un formato, cerca nel testo del PDF i nomi
    di eventi e luoghi già noti a DB tra le performance vicine alla data del biglietto.
    """
    if not full_text or not event_dt:
        return None
    text_key = _normalize_key(full_text)
    if not text_key:
        return None
    text_tokens = {t for t in re.split(r"[^a-z0-9]+", full_text.lower()) if len(t) >= 3}

    def name_matches(name: str) -> bool:
        key = _normalize_key(name)
        if not key:
            return False
        if key in text_key:
            return True
        # nomi a DB più lunghi di quelli sul biglietto: basta il 60% delle parole
        tokens = {t for t in re.split(r"[^a-z0-9]+", name.lower()) if len(t) >= 3}
        return bool(tokens) and len(tokens & text_tokens) / len(tokens) >= 0.6

    # la data sul biglietto è in ora locale, a DB è UTC: finestra ampia ma
    # sotto le 24h per non agganciare la replica del giorno dopo
    start = event_dt - timedelta(hours=12)
    end = event_dt + timedelta(hours=12)
    candidates = (
        Performance.objects.select_related("evento", "luogo")
        .filter(starts_at_utc__range=(start, end))
    )

    best = None
    best_rank = None
    for perf in candidates:
        if not name_matches(getattr(perf.evento, "nome_evento", "")):
            continue
        venue_bonus = 1 if name_matches(getattr(perf.luogo, "nome", "")) else 0
        delta = abs((perf.starts_at_utc - event_dt).total_seconds())
        rank = (-venue_bonus, delta)
        if best_rank is None or rank < best_rank:
            best = perf
            best_rank = rank
    return best


def _try_extract_text_names_prices(pdf_bytes: bytes) -> Dict[str, Any]:
    text = _extract_text(pdf_bytes)
    return {
        "text": text,
        "names": _extract_names(text)[:10],
        "prices": _extract_prices(text)[:10],
    }


def _scan_qr_barcodes(pdf_bytes: bytes) -> List[Dict[str, Any]]:
    """
    Render pagine → immagini → pyzbar decode.
    Torna lista items: {page, code_type, code_raw}
    """
    items = []
    if not (convert_from_bytes and Image and zbar_decode):
        return items

    try:
        pages = convert_from_bytes(pdf_bytes, fmt="png", dpi=200)
        for idx, img in enumerate(pages, start=1):
            # prova 4 rotazioni
            for rot in (0, 90, 180, 270):
                i2 = img.rotate(rot, expand=True) if rot else img
                dec = zbar_decode(i2)
                if dec:
                    for d in dec:
                        ctype = d.type or "CODE"
                        data = d.data.decode("utf-8", errors="ignore")
                        if data:
                            items.append({"page": idx, "code_type": ctype, "code_raw": data})
                    break  # se hai trovato qualcosa in questa pagina, evita rotazioni extra
    except Exception:
        pass

    return items


@shared_task(bind=True, max_retries=2, default_retry_delay=20)
def parse_ticket_pdf(self, upload_id: int):
    upload = TicketUpload.objects.select_related("biglietto").get(pk=upload_id)
    big = upload.biglietto

    try:
        if big.path_file:
            pdf_bytes = _read_all_bytes(big)
        else:
            # e-ticket via URL: il PDF va scaricato ora
            pdf_bytes = _download_source_pdf(upload.source_url)
            filename = f"eticket_{upload.id}.pdf"
            big.path_file.save(filename, ContentFile(pdf_bytes), save=False)
            if not big.nome_file:
                big.nome_file = filename
        # hash file
        hf = _safe_sha256(pdf_bytes)
        if not big.hash_file:
            big.hash_file = hf

        # Conteggio pagine
        pages = _pdf_pages_count(pdf_bytes) or 0
        big.pages_count = pages

        # testo → nominativi/prezzi + metadati ticket
        txt = _try_extract_text_names_prices(pdf_bytes)
        big.extracted_names = txt["names"] or None
        big.extracted_prices = [str(price) for price in (txt["prices"] or [])] or None
        full_text = txt.get("text") or ""

        selected_perf = getattr(big, "performance", None)
        if selected_perf is not None and not getattr(selected_perf, "evento_id", None):
            selected_perf = Performance.objects.select_related("evento", "luogo").filter(pk=selected_perf.pk).first()

        meta = _extract_event_meta(full_text)
        event_name = meta.get("event_name") or (
            getattr(getattr(selected_perf, "evento", None), "nome_evento", None) if selected_perf else None
        )
        venue = meta.get("venue") or (
            getattr(getattr(selected_perf, "luogo", None), "nome", None) if selected_perf else None
        )
        event_dt = meta.get("event_date") or (getattr(selected_perf, "starts_at_utc", None) if selected_perf else None)
        ticket_rows = _build_ticket_rows(full_text)
        codes = _scan_qr_barcodes(pdf_bytes)

        perf = (
            selected_perf
            or _find_matching_performance(event_name, venue, event_dt)
            or _find_performance_in_text(full_text, event_dt)
        )
        if perf is None:
            raise RuntimeError("evento non riconosciuto automaticamente: seleziona l'evento dal menu prima di caricare il biglietto")

        if perf.starts_at_utc <= timezone.now():
            raise RuntimeError("evento gia passato")

        if not ticket_rows and not codes:
            raise RuntimeError("nessun identificativo ticket valido trovato nel PDF")

        big.evento = perf.evento
        big.performance = perf
        big.sigillo_fiscale = next((row["sigillo"] for row in ticket_rows if row.get("sigillo")), None)
        big.qr_code = (
            next((row["ticket_id"] for row in ticket_rows if row.get("ticket_id")), None)
            or next((code.get("code_raw") for code in codes if code.get("code_raw")), None)
        )
        big.extracted_meta = {
            "event_name": event_name,
            "venue": venue,
            "event_date_iso": event_dt.isoformat() if event_dt else None,
            "requires_confirmation": True,
            "ticket_count": len(ticket_rows),
            "ticket_ids": [row.get("ticket_id") for row in ticket_rows if row.get("ticket_id")],
            "sigilli": [row.get("sigillo") for row in ticket_rows if row.get("sigillo")],
        }

        parsed_subitems = _serialize_subitems_for_upload(ticket_rows, codes)
        with transaction.atomic():
            # aggiorna big + upload
            total = len(parsed_subitems)
            if total == 0 and (codes or ticket_rows):
                raise RuntimeError("nessun identificativo ticket valido trovato")

            big.tickets_found = total
            big.is_valid = bool(total and perf)
            big.save()

            upload.found_count = total
            upload.selectable_count = total
            upload.extracted_subitems = parsed_subitems
            upload.status = "READY" if big.is_valid else "ERROR"
            upload.error_message = None if big.is_valid else "Dati ticket insufficienti o evento non riconosciuto"
            upload.save()

    except Exception as e:
        upload.status = "ERROR"
        upload.error_message = str(e)[:500]
        upload.save()
        raise
