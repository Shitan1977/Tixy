from django.contrib import admin
from .models import *

@admin.register(UserProfile)
class UserAdmin(admin.ModelAdmin):
    list_display = ['email',
                    'first_name',
                    'last_name',
                    'otp_code',
                    'otp_created_at',
                    'phone_number',
                    'date_of_birth',
                    'gender',
                    'country',
                    'city',
                    'address',
                    'zip_code',
                    'document_id',
                    'notify_email',
                    'notify_whatsapp',
                    'notify_push',
                    'accepted_terms',
                    'accepted_privacy',
                    'is_active',
                    'is_staff',
                    'created_at',
                    'updated_at']

@admin.register(Recensione)
class RecensioneAdmin(admin.ModelAdmin):
    list_display = ['testo','venditore','acquirente']

@admin.register(Artista)
class ArtistaAdmin(admin.ModelAdmin):
    list_display = ['nome','nome_normalizzato','tipo','nomi_alternativi','creato_il','aggiornato_il']

@admin.register(Luoghi)
class LuoghiAdmin(admin.ModelAdmin):
    list_display = ['nome','nome_normalizzato','indirizzo','citta','citta_normalizzata','stato_iso','creato_il','aggiornato_il']

@admin.register(Categoria)
class CategoriaAdmin(admin.ModelAdmin):
    list_display = ['slug','nome']

@admin.register(Evento)
class EventoAdmin(admin.ModelAdmin):
    list_display = ['slug',
                    'nome_evento',
                    'nome_evento_normalizzato',
                    'descrizione',
                    'data_inizio_utc',
                    'data_fine_utc',
                    'apertura_porte',
                    'stato',
                    'genere',
                    'lingua',
                    'immagine_url',
                    'luogo',
                    'artista_principale',
                    'prezzo_min',
                    'prezzo_max',
                    'valuta',
                    'disponibilita',
                    'categoria',
                    'hash_canonico',
                    'note_raw',
                    'creato_il',
                    'aggiornato_il']

@admin.register(Piattaforma)
class PiattaformaAdmin(admin.ModelAdmin):
    list_display = ['nome','dominio','attivo']

@admin.register(EventoPiattaforma)
class EventoPiattaformaAdmin(admin.ModelAdmin):
    list_display = ['evento',
                    'piattaforma',
                    'id_evento_piattaforma',
                    'url',
                    'ultima_scansione',
                    'snapshot_raw',
                    'checksum_dati',
                    'creato_il',
                    'aggiornato_il']

@admin.register(Sconti)
class ScontiAdmin(admin.ModelAdmin):
    list_display = ['durata_mesi','percentuale','descrizione']

@admin.register(Abbonamento)
class AbbonamentoAdmin(admin.ModelAdmin):
    list_display = ['utente','sconto','prezzo','data_inizio','data_fine','attivo']

@admin.register(Monitoraggio)
class MonitoraggioAdmin(admin.ModelAdmin):
    list_display = ['abbonamento','evento','frequenza_secondi','creato_il', 'aggiornato_il']

@admin.register(Notifica)
class NotificaAdmin(admin.ModelAdmin):
    list_display = ['monitoraggio','tipo','data_invio','esito','testo']

@admin.register(Biglietto)
class BigliettoAdmin(admin.ModelAdmin):
    list_display = [
            'nome_file',
            'nome_intestatario',
            'sigillo_fiscale',
            'path_file',
            'hash_file',
            'is_valid',
            'creato_il',
            'aggiornato_il']

@admin.register(Rivendita)
class RivenditaAdmin(admin.ModelAdmin):
    list_display = ['evento','venditore','biglietto','url','prezzo','disponibile','creato_il','aggiornato_il']

@admin.register(Acquisto)
class AcquistoAdmin(admin.ModelAdmin):
    list_display = ['rivendita','acquirente','data_acquisto','stato','creato_il','aggiornato_il']



