from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import Notifica, PushDevice
from .notifications import send_expo_push_bulk


@receiver(post_save, sender=Notifica)
def notifica_send_push(sender, instance, created, **kwargs):
    if not created or instance.channel != "push":
        return
    try:
        utente = instance.monitoraggio.abbonamento.utente
        if not utente.notify_push:
            return
        tokens = list(
            PushDevice.objects.filter(utente=utente, is_active=True)
            .values_list("token", flat=True)
        )
        if not tokens:
            return
        title = "Tixy Alert"
        body = instance.message or "Nuova notifica disponibile"
        send_expo_push_bulk(tokens=tokens, title=title, body=body, data={"type": "alert"})
    except Exception:
        pass
