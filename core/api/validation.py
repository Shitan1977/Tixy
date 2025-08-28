from django.core.exceptions import ValidationError
from django.utils import timezone as dj_tz
from datetime import datetime, timezone
import pikepdf
import re

def date_check (created,modified):
    now = dj_tz.now()
    if not created and not modified:
        raise ValidationError(f'Il file non possiede metadati.')
    if created and created > now:
        raise ValidationError(f'La data_creazione del file è nel futuro.')
    if modified and modified > now:
        raise ValidationError(f'La data_modifica del file è nel futuro.')
    if created and modified and modified < created:
        raise ValidationError(f'La data_modifica del file è prima della data_creazione.')

def parse_pdf_date(date_str):
    if not date_str:
        return None
    date_str = str(date_str)
    m = re.match(r"D:(\d{14})", date_str)
    if not m:
        return None
    return datetime.strptime(m.group(1), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)

def pdf_validation(file):
    try:
        with pikepdf.open(file) as pdf:
            meta = pdf.docinfo
            created_raw = meta.get('/CreationDate')
            modified_raw = meta.get('/ModDate')
            created = parse_pdf_date(created_raw)
            modified = parse_pdf_date(modified_raw)
            date_check(created,modified)

    except (pikepdf.PdfError, OSError) as e:
        raise ValidationError(f'Errore nella validazione della data del file: {str(e)}')
