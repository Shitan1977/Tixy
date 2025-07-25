import pikepdf
from PIL import Image
from PIL.ExifTags import TAGS
import magic
from datetime import datetime

def processo_validazione(instance):
    file_path = instance.file.path
    mime_type = detect_file_type(file_path)
    errors = []
    extracted_date = None
    is_valid = False

    if mime_type == 'pdf':
        extracted_date, errors = validazione_pdf(file_path)
        instance.file_type = 'pdf'
    elif mime_type.startswith('image'):
        extracted_date, errors = validazione_image(file_path)
        instance.file_type = 'image'
    else:
        errors.append("Unsupported file type.")

    if extracted_date:
        is_valid = True

    instance.extracted_date = extracted_date
    instance.is_valid = is_valid
    instance.validation_errors = "; ".join(errors)
    instance.save()
    return instance

def general(path_file):
    pass

def validazione_image(file_path):
    pass

def validazione_pdf(file_path):
    pass