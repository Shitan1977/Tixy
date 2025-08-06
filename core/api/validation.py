from django.core.exceptions import ValidationError
from django.utils import timezone as tz
from datetime import datetime, timezone
import pikepdf
import re

def date_check (created,modificated,label):
    try:
        now = tz.now()
        if created is None and modificated is None:
            raise ValidationError(f'{label} - i Metadati sono vuoti.')

        if not created and not modificated:
            raise ValidationError(f'{label} - i Metadati sono vuoti.')

        if created and created > now:
            raise ValidationError(f'{label} - la Data di Creazione è nel futuro.')

        if modificated and modificated > now:
            raise ValidationError(f'{label} - la Data di Modifica è nel futuro.')

        if created and modificated and modificated < created:
            raise ValidationError(f'{label} - la Data di Modifica precede la Data di Creazione')

    except OSError as e:
        raise ValidationError(f'Error validation.date_check: {str(e)}')

def parse_pdf_date(date_str):
    try:
        if not date_str:
            return None
        date_str = str(date_str)

        m = re.match(r"D:(\d{14})", date_str)
        if not m:
            return None
        return datetime.strptime(m.group(1), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)

    except (ValueError, TypeError, OSError) as e:
        raise ValidationError(f'Error validation.parse_pdf_date: {str(e)}')

def pdf_validation(file):
    try:
        with pikepdf.open(file) as pdf:
            meta = pdf.docinfo

            created_raw = meta.get('/CreationDate')
            modificated_raw = meta.get('/ModDate')

            created = parse_pdf_date(created_raw)
            modificated = parse_pdf_date(modificated_raw)

            date_check(created,modificated,'PDF')

    except (pikepdf.PdfError, OSError) as e:
        raise ValidationError(f'Error validation.pdf_validation: {str(e)}')
