from django.db.models.signals import post_delete, pre_save
from django.dispatch import receiver
from .models import Biglietto
from .filesystem import remove_empty_dirs
import os

@receiver(post_delete, sender=Biglietto)
def cleanup_on_delete(sender, instance, **kwargs):
    print("Signal post_delete triggered for Biglietto")  # test visivo
    try:
        file_path = instance.path_file.path
        dir_path = os.path.dirname(file_path)
        remove_empty_dirs(dir_path, stop_at='media')
    except Exception as e:
        raise f'Errore signals.cleanup_on_delete: {str(e)}'


@receiver(pre_save, sender=Biglietto)
def cleanup_on_replace(sender, instance, **kwargs):
    try:
        if not instance.pk:
            return

        old_instance = sender.objects.get(pk=instance.pk)

        if old_instance.path_file and old_instance.path_file != instance.path_file:
            old_file_path = old_instance.path_file.path
            if os.path.isfile(old_file_path):
                os.remove(old_file_path)

            old_dir_path = os.path.dirname(old_file_path)
            remove_empty_dirs(old_dir_path, stop_at='media')

    except sender.DoesNotExist:
        return 'Il record non esiste.'
    except Exception as e:
        raise f'Errore signals.cleanup_on_replace: {str(e)}'