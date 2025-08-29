import os.path
from django.core.exceptions import ValidationError
from datetime import datetime
import pikepdf
import re

def date_check (created,modified):
    now = datetime.now()
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
    return datetime.strptime(m.group(1), "%Y%m%d%H%M%S")

def check_meta(file):
    try:
        with pikepdf.open(file.file) as pdf:
            meta = pdf.docinfo
            print(meta)
            created_raw = meta.get('/CreationDate')
            modified_raw = meta.get('/ModDate')
            created = parse_pdf_date(created_raw)
            modified = parse_pdf_date(modified_raw)
            date_check(created, modified)

    except pikepdf.PdfError as e:
        raise ValidationError(f'Errore nel controllo dei metadati: {str(e)}')

def pdf_validation(file):
    try:
        all_exe = ('.pdf',)
        base, exe = os.path.splitext(file.name)
        exe = exe.lower()
        if exe not in all_exe:
            raise ValidationError(f'Il file non è un pdf')
        check_meta(file)

    except (OSError, ValidationError) as e:
        raise ValidationError(f'Errore durante validazione: {str(e)}')
