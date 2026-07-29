from django.conf import settings
from django.core.mail import send_mail

FROM_EMAIL = "supporto@tixy.it"


def invia_otp_email(user):
    subject = "Conferma registrazione – Tixy"
    message = f"Ciao {user.first_name},\n\nIl tuo codice di verifica è: {user.otp_code}\n\nScade tra 10 minuti.\n\nGrazie!"
    send_mail(subject, message, FROM_EMAIL, [user.email])


def invia_email_venditore_vendita(order, deadline):
    """
    Notifica il venditore che il biglietto è stato acquistato.
    Ha TIXY_SELLER_UPLOAD_DEADLINE_HOURS ore per caricare il PDF aggiornato
    con nome e sigillo fiscale modificati.
    """
    seller = order.listing.seller
    buyer = order.buyer
    deadline_hours = int(getattr(settings, "TIXY_SELLER_UPLOAD_DEADLINE_HOURS", 12))
    deadline_str = deadline.strftime("%d/%m/%Y %H:%M") if deadline else f"entro {deadline_hours} ore"

    holder_names = [n for n in (getattr(order, "holder_names", None) or []) if n]
    holders_block = ""
    if holder_names:
        holders_block = (
            "Nuovi intestatari comunicati dall'acquirente (da riportare sul biglietto):\n"
            + "\n".join(f"  {i}. {name}" for i, name in enumerate(holder_names, start=1))
            + "\n\n"
        )

    subject = "Il tuo biglietto è stato venduto – Tixy"
    message = (
        f"Ciao {seller.first_name},\n\n"
        f"Il tuo annuncio (ordine #{order.id}) è stato acquistato da {buyer.first_name} {buyer.last_name}.\n\n"
        f"{holders_block}"
        f"Devi caricare il biglietto aggiornato (con nome intestatario e sigillo fiscale cambiati) "
        f"entro: {deadline_str}\n\n"
        f"Accedi alla tua area riservata > Le mie rivendite e usa il pulsante 'Carica biglietto' "
        f"accanto all'ordine #{order.id}.\n\n"
        f"Se non carichi il biglietto entro la scadenza, l'ordine verrà annullato "
        f"automaticamente, l'acquirente rimborsato e il tuo annuncio rimesso in vendita.\n\n"
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


def invia_email_acquirente_ordine_annullato(order):
    """
    Notifica l'acquirente che l'ordine è stato annullato perché il venditore
    non ha caricato il biglietto rinominato entro la scadenza, e che verrà
    rimborsato.
    """
    buyer = order.buyer
    subject = "Il tuo ordine è stato annullato e rimborsato – Tixy"
    message = (
        f"Ciao {buyer.first_name},\n\n"
        f"Il venditore non ha caricato in tempo il biglietto aggiornato per il tuo "
        f"ordine #{order.id}, quindi l'abbiamo annullato automaticamente.\n\n"
        f"Riceverai il rimborso dell'importo pagato ({order.final_total or order.total_price} {order.currency}).\n\n"
        f"Ci scusiamo per il disagio.\n\nTeam Tixy"
    )
    send_mail(subject, message, FROM_EMAIL, [buyer.email])


def invia_email_venditore_ordine_annullato(order):
    """
    Notifica il venditore che l'ordine è stato annullato per mancata consegna
    entro la scadenza, e che il suo annuncio è tornato disponibile.
    """
    seller = order.listing.seller
    subject = "Vendita annullata per mancata consegna – Tixy"
    message = (
        f"Ciao {seller.first_name},\n\n"
        f"Non hai caricato in tempo il biglietto aggiornato per l'ordine #{order.id}, "
        f"quindi l'abbiamo annullato e l'acquirente è stato rimborsato.\n\n"
        f"Il tuo annuncio è stato rimesso in vendita.\n\n"
        f"Team Tixy"
    )
    send_mail(subject, message, FROM_EMAIL, [seller.email])
