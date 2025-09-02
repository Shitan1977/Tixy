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
           for chunk in file.chunks():
               file_temporaneo.write(chunk)
           file_temporaneo.flush()
           path_temporaneo = file_temporaneo.name

       check_meta(path_temporaneo)
       sigillo = leggi_file(path_temporaneo)

       return sigillo

    except (OSError, ValidationError) as e:
        raise ValidationError(f'Errore durante validazione: {str(e)}')
    finally:
        if path_temporaneo and os.path.exists(path_temporaneo):
            os.remove(path_temporaneo)

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

def leggi_file(path_temporaneo):
    try:
        with open(path_temporaneo, 'rb') as text:
            testo = extract_text(text)
        print(repr(testo))
        biglietti = []

        # --- REGEX TICKETONE ---
        # Esempio: "Sigillo Fiscale: 2e19b61b7dd31a59 ... posto7CampoMartinaPrezzo"
        pattern_ticketone = re.compile(
            r"Sigillo Fiscale:\s*([0-9a-f]+).*?posto\d+([A-Za-zÀ-ÖØ-öø-ÿ\s]+?)\s*Prezzo",
            flags=re.DOTALL
        )

        # --- REGEX TICKETMASTER ---
        # Esempio: "SF: cfb4873ec4c90c5d\nStefano Barrancotto\nPI Org.: ..."
        pattern_ticketmaster = re.compile(
            r"(?:SF|S\.F\.?|Sigillo Fiscale)[:\s]*([0-9a-f]+).*?(?:\n|\r)([A-Z][A-Za-zÀ-ÖØ-öø-ÿ\s']+?)\s*(?:PI|Progressivo|Emissione|Prezzo)",
            flags=re.DOTALL
        )

        # --- CERCA IN TUTTI I FORMATI ---
        for sigillo, nome in pattern_ticketone.findall(testo):
            nome_pulito = re.sub(r"\s+", " ", nome).strip().title()
            biglietti.append({
                "sigillo": sigillo.strip(),
                "intestatario": nome_pulito
            })

        for sigillo, nome in pattern_ticketmaster.findall(testo):
            nome_pulito = re.sub(r"\s+", " ", nome).strip().title()
            biglietti.append({
                "sigillo": sigillo.strip(),
                "intestatario": nome_pulito
            })

        # --- RIMUOVI DUPLICATI (se stesso sigillo compare più volte)
        unici = {}
        for b in biglietti:
            unici[b["sigillo"]] = b  # sovrascrive eventuali duplicati
        biglietti = list(unici.values())

        print(biglietti)

        return sigillo.strip()


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