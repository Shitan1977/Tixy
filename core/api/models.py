from datetime import datetime, timedelta
from random import choices

from django.db.transaction import mark_for_rollback_on_error
from django.template.defaultfilters import default
from django.utils import timezone
from django.db import models
from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
import os
import re

class UserProfileManager(BaseUserManager):
    def create_user(self, email, password=None, **extra_fields):
        if not email:
            raise ValueError('Email obbligatoria')
        email = self.normalize_email(email)
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)
        return self.create_user(email, password, **extra_fields)

class UserProfile(AbstractBaseUser, PermissionsMixin):
    email = models.EmailField(unique=True)
    first_name = models.CharField(max_length=100)
    otp_code = models.CharField(max_length=6, blank=True, null=True)
    otp_created_at = models.DateTimeField(blank=True, null=True)
    last_name = models.CharField(max_length=100)
    phone_number = models.CharField(max_length=20, blank=True, null=True)
    date_of_birth = models.DateField(blank=True, null=True)
    gender = models.CharField(
        max_length=20,
        choices=[
            ('male', 'Maschio'),
            ('female', 'Femmina'),
            ('other', 'Altro'),
            ('na', 'Preferisco non dirlo')
        ],
        default='na'
    )
    country = models.CharField(max_length=100, blank=True, null=True)
    city = models.CharField(max_length=100, blank=True, null=True)
    address = models.CharField(max_length=255, blank=True, null=True)
    zip_code = models.CharField(max_length=20, blank=True, null=True)
    document_id = models.CharField(max_length=50, blank=True, null=True)

    notify_email = models.BooleanField(default=True)
    notify_whatsapp = models.BooleanField(default=False)
    notify_push = models.BooleanField(default=True)

    accepted_terms = models.BooleanField(default=False)
    accepted_privacy = models.BooleanField(default=False)

    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)  # Può accedere all'admin
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = UserProfileManager()

    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = ['first_name', 'last_name']

    def generate_otp(self):
        import random
        self.otp_code = str(random.randint(100000, 999999))
        self.otp_created_at = timezone.now()
        self.save()
        return self.otp_code

    def is_otp_valid(self, code):
        if self.otp_code != code:
            return False
        if not self.otp_created_at:
            return False
        if timezone.now() > self.otp_created_at + timedelta(minutes=10):
            return False
        return True
    def __str__(self):
        return f"{self.first_name} {self.last_name} ({self.email})"

class Recensione(models.Model):
    testo = models.TextField()
    venditore = models.ForeignKey(UserProfile,
                                     on_delete=models.CASCADE,
                                     blank=True, null=True,
                                     related_name='venditore')
    acquirente = models.ForeignKey(UserProfile,
                                      on_delete=models.SET_NULL,
                                      blank=True, null=True,
                                      related_name='acquirente')

class Artista(models.Model):
    nome = models.CharField(max_length=255,blank=True, null=True)
    nome_normalizzato = models.CharField(max_length=255,blank=True, null=True,unique=True)
    tipo = models.CharField(max_length=7, choices=[
                                ('artista','Artista'),
                                ('squadra','Squadra'),
                                ('atleta','Atleta'),
                                ('altro','Altro')
                            ], default='artista')
    nomi_alternativi = models.JSONField(blank=True, null=True)
    creato_il = models.DateTimeField(auto_now_add=True)
    aggiornato_il = models.DateTimeField(auto_now=True)

class Luoghi(models.Model):
    nome = models.CharField(max_length=255, default='')
    nome_normalizzato = models.CharField(max_length=255, default='')
    indirizzo = models.CharField(max_length=255,blank=True, null=True)
    citta = models.CharField(max_length=120, blank=True, null=True)
    citta_normalizzata = models.CharField(max_length=120, blank=True, null=True)
    stato_iso = models.CharField(max_length=2,blank=True, null=True)
    creato_il= models.DateTimeField(auto_now_add=True)
    aggiornato_il = models.DateTimeField(auto_now=True)

class Categoria(models.Model):
    slug = models.CharField(max_length=60,default='',unique=True)
    nome = models.CharField(max_length=120,default='')

class Evento(models.Model):
    STATO = [
        ('pianificato', 'Pianificato'),
        ('annullato', 'Annullato'),
        ('rinviato', 'Rinviato'),
        ('riprogrammato', 'Riprogrammato'),
    ]

    DISPONIBILITA = [
        ('disponibile', 'Disponibile'),
        ('sconosciuti', 'Sconosciuti'),
        ('limitata','Limitata'),
        ('non_disponibile', 'Non Disponibile'),
    ]

    slug = models.CharField(max_length=255,default='')
    nome_evento = models.CharField(max_length=255,default='')
    nome_evento_normalizzato = models.CharField(max_length=255,default='')
    descrizione = models.TextField(blank=True,null=True)
    data_inizio_utc = models.DateTimeField()
    data_fine_utc = models.DateTimeField(blank=True, null=True)
    apertura_porte = models.DateTimeField(blank=True, null=True)
    stato = models.CharField(max_length=50, choices=STATO,default='pianificato')
    genere = models.CharField(max_length=120,blank=True, null=True)
    lingua = models.CharField(max_length=40,blank=True, null=True)
    immagine_url = models.CharField(max_length=516,blank=True, null=True)
    luogo = models.ForeignKey(Luoghi,
                                 on_delete=models.SET_NULL,
                                 blank=True, null=True,
                                 related_name='luogo_evento')
    artista_principale = models.ForeignKey(Artista,
                                 on_delete=models.SET_NULL,
                                 blank=True, null=True,
                                 related_name='artista_evento')
    prezzo_min = models.DecimalField(max_digits=10,decimal_places=2,blank=True,null=True)
    prezzo_max = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True)
    valuta = models.CharField(max_length=3,blank=True, null=True)
    disponibilita = models.CharField(max_length=20, choices=DISPONIBILITA, default='disponibile')
    categoria = models.ForeignKey(Categoria,
                                     on_delete=models.SET_NULL,
                                     blank=True, null=True,
                                     related_name='categoria_evento')
    hash_canonico = models.CharField(max_length=64,default='',unique=True)
    note_raw = models.JSONField(blank=True,null=True)
    creato_il= models.DateTimeField(auto_now_add=True)
    aggiornato_il = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.nome_evento} - {self.data_inizio_utc.strftime('%d/%m/%Y')}"

    class Meta:
        ordering = ['-data_ora']

class Piattaforma(models.Model):
    nome = models.CharField(max_length=60,default='', unique=True)
    dominio = models.CharField(max_length=120,blank=True, null=True)
    attivo = models.BooleanField(default=True)

    def __str__(self):
        return self.nome

    class Meta:
        verbose_name_plural = "Piattaforme"

class EventoPiattaforma(models.Model):
    evento = models.ForeignKey(Evento,
                               on_delete=models.CASCADE,
                               related_name='evento')
    piattaforma = models.ForeignKey(Piattaforma,
                                    on_delete=models.CASCADE,
                                    related_name='piattaforma')
    id_evento_piattaforma = models.CharField(max_length=255,blank=True, null=True)
    url = models.CharField(max_length=1024,default='')
    disponibilita_piattaforma = models.CharField(max_length=20,
                                                 choices=[('disponibile','Disponibile'),
                                                          ('limitata','Limitata'),
                                                          ('non_disponibile','Non Disponibile'),
                                                          ('sconosciuta','Sconosciuta')],
                                                 default='disponibile')
    visto_il = models.DateTimeField()
    snapshot_raw = models.JSONField(blank=True,null=True)
    checksum_dati = models.CharField(max_length=64, null=True, blank=True)
    creato_il = models.DateTimeField(auto_now_add=True)
    aggiornato_il = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.evento.nome_evento} su {self.piattaforma.nome}"

    class Meta:
        unique_together = ('evento', 'piattaforma')

class Sconti(models.Model):
    durata_mesi = models.IntegerField()
    percentuale = models.IntegerField()
    descrizione = models.TextField(blank=True,null=True)

class Abbonamento(models.Model):
    utente = models.ForeignKey(UserProfile,
                               on_delete=models.CASCADE,
                               related_name='utente')
    data_inizio = models.DateTimeField(default=timezone.now)
    data_fine = models.DateTimeField(blank=True,null=True)
    sconto = models.ForeignKey(Sconti,
                               on_delete=models.SET_NULL,
                               null=True,
                               related_name='sconto')
    prezzo = models.DecimalField(max_digits=10,decimal_places=2,default=0.0)
    attivo = models.BooleanField(default=True)

class Monitoraggio(models.Model):
    abbonamento = models.ForeignKey(Abbonamento,
                                    on_delete=models.CASCADE,
                                    related_name='abbonamento')
    evento = models.ForeignKey(Evento,
                                on_delete=models.CASCADE,
                                related_name='evento')
    frequenza_secondi = models.IntegerField(default=5)
    creato_il= models.DateTimeField(auto_now_add=True)
    aggiornato_il = models.DateTimeField(auto_now=True)

class Notifica(models.Model):
    monitoraggio = models.ForeignKey(Monitoraggio,
                                     on_delete=models.CASCADE,
                                     related_name='monitoraggio')
    tipo = models.CharField(max_length=10,choices=[('email'),('push')], default='push')
    data_invio = models.DateTimeField(default=timezone.now)
    esito = models.CharField(max_length=10, choices=[('successo'),('errore')])
    testo = models.TextField()

# BIGLIETTO
def biglietto_path(instance,filename):
    return f"uploads/{datetime.now().strftime('%Y/%m')}/{filename}"

class Biglietto(models.Model):
    nome_file = models.CharField(max_length=255,blank=True,null=True)
    nome_intestatario = models.CharField(max_length=255,blank=True,null=True)
    sigillo_fiscale = models.CharField(max_length=255,blank=True, null=True)
    path_file = models.FileField(upload_to=biglietto_path)
    hash_file = models.CharField(max_length=64, blank=True,null=True)
    is_valid = models.BooleanField(default=False)
    data_caricamento = models.DateTimeField(auto_now_add=True)

    def save(self,*args,**kwargs):
        if not self.nome_file and self.path_file:
            raw_name = os.path.basename(self.path_file.name)
            safe_name = re.sub(r'[^a-zA-Z0-9._-]', '_', raw_name)
            self.nome_file = safe_name
            self.is_valid=False
        super().save(*args,**kwargs)

class Rivendita(models.Model):
    evento = models.ForeignKey(Evento,
                               on_delete=models.CASCADE,
                               related_name='evento')
    venditore = models.ForeignKey(UserProfile,
                                  null=True,
                                  on_delete=models.SET_NULL,
                                  related_name='venditore')
    biglietto = models.ForeignKey(Biglietto,
                                  on_delete=models.CASCADE,
                                  related_name='biglietto')
    url = models.CharField(max_length=1024, default='')
    prezzo = models.DecimalField(max_digits=10,decimal_places=2,default=0.0)
    disponibile = models.BooleanField(default=True)
    data_caricamento = models.DateTimeField(auto_now_add=True)

class Acquisto(models.Model):
    rivendita = models.ForeignKey(Rivendita,
                                  on_delete=models.CASCADE,
                                  related_name='rivendita')
    acquirente = models.ForeignKey(UserProfile,
                                   on_delete=models.CASCADE,
                                   related_name='acquirente')
    data_acquisto = models.DateTimeField(auto_now_add=True)
    stato = models.CharField(max_length=10, choices=[('in_corso'),('completato'),('rimborsato')], default='in_corso')
