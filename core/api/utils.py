from django.core.mail import send_mail

FROM_EMAIL = "noreply@misteralert.it"


def invia_otp_email(user):
    subject = "Conferma registrazione – Tixy"
    message = f"Ciao {user.first_name},\n\nIl tuo codice di verifica è: {user.otp_code}\n\nScade tra 10 minuti.\n\nGrazie!"
    send_mail(subject, message, FROM_EMAIL, [user.email])


def invia_email_venditore_vendita(order, deadline):
    """
    Notifica il venditore che il biglietto è stato acquistato.
    Ha 24 ore per caricare il PDF aggiornato con nome e sigillo fiscale modificati.
    """
    seller = order.listing.seller
    buyer = order.buyer
    deadline_str = deadline.strftime("%d/%m/%Y %H:%M") if deadline else "entro 24 ore"
    subject = "Il tuo biglietto è stato venduto – Tixy"
    message = (
        f"Ciao {seller.first_name},\n\n"
        f"Il tuo annuncio (ordine #{order.id}) è stato acquistato da {buyer.first_name} {buyer.last_name}.\n\n"
        f"Devi caricare il biglietto aggiornato (con nome intestatario e sigillo fiscale cambiati) "
        f"entro: {deadline_str}\n\n"
        f"Accedi alla tua area riservata > Le mie rivendite e usa il pulsante 'Carica biglietto' "
        f"accanto all'ordine #{order.id}.\n\n"
        f"Se non carichi il biglietto entro la scadenza, l'ordine potrebbe essere annullato.\n\n"
        f"Grazie,\nTeam Tixy"
    )
    send_mail(subject, message, FROM_EMAIL, [seller.email])


def invia_email_acquirente_consegna(order):
    """
    Notifica l'acquirente che il biglietto aggiornato è disponibile per il download.
    """
    buyer = order.buyer
    subject = "Il tuo biglietto è pronto – Tixy"
    message = (
        f"Ciao {buyer.first_name},\n\n"
        f"Il venditore ha caricato il biglietto aggiornato per l'ordine #{order.id}.\n\n"
        f"Puoi scaricarlo dalla tua area riservata > I miei biglietti.\n\n"
        f"Grazie per aver acquistato su Tixy!\n\nTeam Tixy"
    )
    send_mail(subject, message, FROM_EMAIL, [buyer.email])
