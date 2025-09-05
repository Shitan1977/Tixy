import hashlib
import os.path
from django.core.exceptions import ValidationError
from datetime import datetime
import re
import pikepdf
import tempfile
from pdfminer.high_level import extract_text

def file_validation(file):
    path_temporaneo = None
    try:
       with tempfile.NamedTemporaryFile(delete=False,suffix=".pdf") as file_temporaneo:
           if hasattr(file, "chunks"):
                for chunk in file.chunks():
                   file_temporaneo.write(chunk)
           else:
               file_temporaneo.write(file.read())
           file_temporaneo.flush()
           path_temporaneo = file_temporaneo.name

       hash_file = genera_hash(path_temporaneo)
       check_meta(path_temporaneo)
       sigilli = trova_sigilli(path_temporaneo)

       return sigilli, hash_file

    except (OSError, ValidationError) as e:
        raise ValidationError(f'Errore durante validazione: {str(e)}')
    finally:
        if path_temporaneo and os.path.exists(path_temporaneo):
            os.remove(path_temporaneo)

def genera_hash(file):
    h = hashlib.sha256()
    with open(file, 'rb') as file:
        for chunk in iter(lambda : file.read(8192), b""):
            h.update(chunk)
        return h.hexdigest()

def check_meta(file):
    try:
        with pikepdf.open(file) as pdf:
            meta = pdf.docinfo
            print(meta)
            created_raw = meta.get('/CreationDate')
            modified_raw = meta.get('/ModDate')
            created = parse_pdf_date(created_raw)
            modified = parse_pdf_date(modified_raw)
            print(f'CREATO: {created}\nMODIFICATO: {modified}')
            date_check(created, modified)

    except pikepdf.PdfError as e:
        raise ValidationError(f'Errore nel controllo dei metadati: {str(e)}')

def trova_sigilli(path_temporaneo):
    try:
        with open(path_temporaneo, 'rb') as t:
            testo = extract_text(t)
        print(repr(testo))

        # --- REGEX TICKETONE ---
        pattern_ticketone = re.compile(r"Sigillo Fiscale:\s*([0-9a-f]+)")
        sigilli = pattern_ticketone.findall(testo)

        # --- RIMUOVE DUPLICATI e MANTIENE L'ORDINE DI ESTRAZIONE
        sigilli_unici = list(dict.fromkeys(sigilli))

        print(sigilli_unici)
        return sigilli_unici

    except Exception as e:
        raise ValidationError(f'Errore nella lettura del test: {str(e)}')


# Controllo sulla data
def parse_pdf_date(date_str):
    if not date_str:
        return None
    date_str = str(date_str)
    m = re.match(r"D:(\d{14})", date_str)
    if not m:
        return None
    return datetime.strptime(m.group(1), "%Y%m%d%H%M%S")

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