from django.core.mail import send_mail

def invia_otp_email(user):
    subject = "Conferma registrazione – Tixy"
    message = f"Ciao {user.first_name},\n\nIl tuo codice di verifica è: {user.otp_code}\n\nScade tra 10 minuti.\n\nGrazie!"
    from_email = 'noreply@misteralert.it'  # Assicurati che sia un indirizzo valido
    recipient_list = [user.email]

    send_mail(subject, message, from_email, recipient_list)
