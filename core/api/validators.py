from django.conf import settings
from django.core.exceptions import ValidationError
from django.utils import timezone
from PIL import Image, ImageOps
from PIL.ExifTags import TAGS
import pikepdf
import puremagic
import re
import os



ALLOWED_MIME_TYPES = {
    'application/pdf':'PDF',
    'image/jpeg':'Image',
    'image/png':'Image',
    'image/webp':'Image',
}

def find_mime(file) -> str:
    try:
        mime_list = puremagic.magic_file(file)
        txt_dir = settings.MEDIA_ROOT
        os.makedirs(txt_dir, exist_ok=True)
        txt_path = os.path.join(txt_dir,'tmp_mime.txt')
        with open(txt_path,'w') as f:
            for m in mime_list:
                f.write(f'{m.mime_type}: confidence {m.confidence}\n')

        for m in mime_list:
            if m.mime_type and 0.85 < m.confidence < 1.00:
                return m.mime_type
    except:
        raise ValidationError('File non accettato')

def get_file_type(file) -> str:
    mime = find_mime(file)
    try:
        return ALLOWED_MIME_TYPES[mime]
    except KeyError:
        raise ValidationError(f'Formato file non ammesso : {mime}')

def validation_process(file):
    type_file = get_file_type(file)

    if type_file == 'PDF':
        pdf_validation(file)
    elif type_file == 'Image':
        image_validation(file)
    else:
        raise ValidationError(f'File non ammesso : {type_file}')

def date_check (created,modificated,label):
    now = timezone.now()
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

def pdf_validation(file):
    try:
        with pikepdf.open(file) as pdf:
            meta = pdf.open_metadata()

            created_raw = meta.get('xmp:CreationDate')
            modificated_raw = meta.get('xmp:ModDate')

            created = parse_pdf_date(created_raw)
            modificated = parse_pdf_date(modificated_raw)

            date_check(created,modificated,'PDF')

    except (pikepdf.PdfError, OSError) as e:
        raise ValidationError(f'Errore durante la lettura del PDF: {str(e)}')


#Validazione del Image
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

def image_validation(file):
    try:
        with Image.open(file) as image:
            image = ImageOps.exif_transpose(image)
            exif= image.getexif()

            if not exif:
                raise ValidationError("Immagine priva di metadati EXIF.")

            created = None
            modificated = None

            for tag_id, value in exif.items():
                tag = TAGS.get(tag_id, tag_id)
                if tag == 'DateTimeOriginal':
                    created = parse_exif_data(value)
                elif tag in ('DateTime', 'DateTimeDigitized'):
                    modificated = parse_exif_data(value)

            date_check(created,modificated,'Image')

    except ValidationError:
        raise
    except (IOError,OSError,Image.DecompressionBombError)as e:
        raise ValidationError(f"Errore di lettura dell'Immagine:{str(e)}")
    except Exception as e:
        raise ValidationError(f"Errore nei metadati EXIF: {str(e)}")
