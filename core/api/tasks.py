from celery import shared_task
from django.db import transaction
from django.core.files.storage import default_storage
from hashlib import sha256
from io import BytesIO
from typing import List, Dict, Any

from .models import TicketUpload, TicketSubitem, Biglietto

# --- helper sicuri: nessuna dipendenza hard obbligatoria ---
try:
    from pypdf import PdfReader
except Exception:
    PdfReader = None

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


def _try_extract_text_names_prices(pdf_bytes: bytes) -> Dict[str, Any]:
    """
    Minimal estrazione 'best effort':
    - prova pypdf per testo,
    - fai semplici regex per 'nome', 'prezzo' eur.
    In produzione puoi sostituire con pdfminer.six + regexp più robuste/multilingua.
    """
    import re
    out_names = []
    out_prices = []
    if not PdfReader:
        return {"names": out_names, "prices": out_prices}

    try:
        reader = PdfReader(BytesIO(pdf_bytes))
        full_text = []
        for p in reader.pages:
            try:
                t = p.extract_text() or ""
                full_text.append(t)
            except Exception:
                pass
        text = "\n".join(full_text)

        # euristiche semplici:
        # "Intestatario: Mario Rossi" / "Nome: ..." / "Holder: ..."
        name_pat = re.compile(r"(?:Intestatario|Nome|Nominativo|Holder)\s*[:\-]\s*([A-Z][^\n]+)", re.I)
        for m in name_pat.finditer(text):
            n = m.group(1).strip()
            if n and n not in out_names:
                out_names.append(n[:120])

        # Prezzi tipo € 95,00  |  95.00 EUR
        price_pat = re.compile(r"(?:€\s*)?(\d{1,4}[.,]\d{2})\s*(?:EUR|€)?", re.I)
        for m in price_pat.finditer(text):
            raw = m.group(1).replace(",", ".")
            try:
                out_prices.append(round(float(raw), 2))
            except Exception:
                pass

    except Exception:
        pass

    return {"names": out_names[:10], "prices": out_prices[:10]}


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
        pdf_bytes = _read_all_bytes(big)
        # hash file
        hf = _safe_sha256(pdf_bytes)
        if not big.hash_file:
            big.hash_file = hf

        # Conteggio pagine
        pages = _pdf_pages_count(pdf_bytes) or 0
        big.pages_count = pages

        # testo → nominativi/prezzi
        txt = _try_extract_text_names_prices(pdf_bytes)
        big.extracted_names = txt["names"] or None
        big.extracted_prices = txt["prices"] or None

        # decoding QR/barcode (best effort)
        codes = _scan_qr_barcodes(pdf_bytes)

        # Se non troviamo QR, proviamo a costruire un "sigillo" dal testo (fallback debolissimo)
        sigillo = big.sigillo_fiscale

        sub_created = 0
        with transaction.atomic():
            # crea subitems da codici
            for c in codes or []:
                raw = c["code_raw"].strip()
                code_hash = _safe_sha256(raw.encode("utf-8"))
                obj, created = TicketSubitem.objects.get_or_create(
                    code_hash=code_hash,
                    defaults=dict(
                        biglietto=big,
                        full_name=(big.extracted_names or [None])[0],
                        price=(big.extracted_prices or [None])[0],
                        page=c.get("page"),
                        code_type=c.get("code_type"),
                        code_raw=raw,
                    ),
                )
                sub_created += 1 if created else 0

            # se nessun QR, crea almeno 1 subitem 'generico' per consentire la vendita manuale
            if sub_created == 0:
                # fallback: usa hash_file per differenziare il biglietto + indice 0
                code_hash = _safe_sha256((hf + "_0").encode("utf-8"))
                TicketSubitem.objects.get_or_create(
                    code_hash=code_hash,
                    defaults=dict(
                        biglietto=big,
                        full_name=(big.extracted_names or [None])[0],
                        price=(big.extracted_prices or [None])[0],
                        page=1 if pages else None,
                        code_type="FALLBACK",
                        code_raw=sigillo or hf[:16],
                    ),
                )
                sub_created = 1

            # aggiorna big + upload
            total = big.subitems.count()
            big.tickets_found = total
            big.is_valid = total > 0
            big.save()

            upload.found_count = total
            upload.selectable_count = total
            upload.status = "READY" if total > 0 else "ERROR"
            upload.error_message = None if total > 0 else "Nessun biglietto identificato"
            upload.save()

    except Exception as e:
        upload.status = "ERROR"
        upload.error_message = str(e)[:500]
        upload.save()
        raise
