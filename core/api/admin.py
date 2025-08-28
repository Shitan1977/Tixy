from django.contrib import admin
from .models import *

@admin.register(Biglietto)
class BigliettoAdmin(admin.ModelAdmin):
    list_display = ['nome','data_caricamento','is_valid','path_file']
@admin.register(Evento)
class EventoAdmin(admin.ModelAdmin):
    list_display = ['nome_evento','descrizione','artista','data_ora','luogo','citta','url_immagine','categoria','stato_disponibilita','attivo','timestamp_aggiornamento']
@admin.register(EventoPiattaforma)
class EventoPiattaformaAdmin(admin.ModelAdmin):
    list_display = ['evento','piattaforma','url_pagina_evento','disponibilita_biglietti','prezzo_minimo','timestamp_aggiornamento']
@admin.register(Piattaforma)
class PiattaformaAdmin(admin.ModelAdmin):
    list_display = ['nome','url_base']
@admin.register(UserProfile)
class EventoAdmin(admin.ModelAdmin):
    list_display = ['email','first_name','otp_code','otp_created_at','last_name','phone_number','date_of_birth','gender','country','city','address','zip_code','document_id','notify_email','notify_whatsapp','notify_push','accepted_terms','accepted_privacy','is_active','is_staff','created_at','updated_at']
