from django.core.exceptions import ValidationError
from django.utils import timezone
from PIL import Image, ImageOps
from PIL.ExifTags import TAGS
import pikepdf
import magic
import re


ALLOWED_MIME_TYPES = {
    'application/pdf':'PDF',
    'image/jpeg':'Image',
    'image/png':'Image',
    'image/webp':'Image',
}

def trova_mime(file) -> str:
    doc = file.read(2048)
    mime = magic.from_buffer(doc,mime=True)
    file.seek(0)
    return mime

def get_file_type(file) -> str:
    mime = trova_mime(file)
    try:
        return ALLOWED_MIME_TYPES[mime]
    except KeyError:
        raise ValidationError('Formato file non ammesso')

def validation_process(file):
    mime = trova_mime(file)

    if mime == 'application/pdf':
        validazione_pdf(file)
    elif mime.startswith('image/'):
        validazione_image(file)
    else:
        raise ValidationError('File non ammesso')

def controllo_data (creato,modificato,label):
    now = timezone.now()
    if creato in None and modificato is None:
        raise ValidationError(f'{label} - i Metadati sono vuoti.')

    if creato and creato > now:
        raise ValidationError(f'{label} - la Data di Creazione è nel futuro.')

    if modificato and modificato > now:
        raise ValidationError(f'{label} - la Data di Modifica è nel futuro.')

    if creato and modificato and modificato < creato:
        raise ValidationError(f'{label} - la Data di Modifica precede la Data di Creazione')

# Validazione dei PDF
def parse_pdf_date(date_str):
    if not date_str:
        return None
    try:
        m = re.match(r"D:(\d{14})", date_str)
        if not m:
            return None
        return timezone.datetime.strptime(m.group(1), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None

def validazione_pdf(file):
    try:
        with pikepdf.open(file) as pdf:
            meta = pdf.open_metadata()

            creato_raw = meta.get('xmp:CreationDate')
            modifica_raw = meta.get('xmp:ModDate')

            creato = parse_pdf_date(creato_raw)
            modificato = parse_pdf_date(modifica_raw)

            controllo_data(creato,modificato,'PDF')

    except (pikepdf.PdfError, OSError) as e:
        raise ValidationError(f'Errore durante la lettura del PDF: {str(e)}')


#Validazione delle Image
def parse_exif_data(date_str):
    if not date_str:
        return None
    try:
        if len(date_str) != 19 or ':' not in date_str:
            return None
        dt = timezone.datetime.strptime(date_str,'%Y:%m:%d %H:%M:%S' )
        return timezone.make_aware(dt)
    except (ValueError,TypeError):
        return None

def validazione_image(file):
    try:
        with Image.open(file) as image:
            image = ImageOps.exif_transpose(image)
            exif= image.getexif()

            if not exif:
                raise ValidationError("Immagine priva di metadati EXIF.")

            creato = None
            modifica = None

            for tag_id, value in exif.items():
                tag = TAGS.get(tag_id, tag_id)
                if tag == 'DateTimeOriginal':
                    creato = parse_exif_data(value)
                elif tag in ('DateTime', 'DateTimeDigitized'):
                    modifica = parse_exif_data(value)

            controllo_data(creato,modifica,'Image')

    except ValidationError:
        raise
    except (IOError,OSError,Image.DecompressionBombError)as e:
        raise ValidationError(f"Errore di lettura dell'Immagine:{str(e)}")
    except Exception as e:
        raise ValidationError(f"Errore nei metadati EXIF: {str(e)}")
