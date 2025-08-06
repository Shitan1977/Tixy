from datetime import timedelta
from django.utils import timezone
from django.db import models
from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.core.exceptions import ValidationError
from .validation import pdf_validation

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

# modello evento

class Evento(models.Model):
    CATEGORIE = [
        ('concerto', 'Concerto'),
        ('teatro', 'Teatro'),
        ('sport', 'Sport'),
        ('altro', 'Altro'),
    ]

    STATO_DISPONIBILITA = [
        ('disponibile', 'Disponibile'),
        ('sold_out', 'Sold Out'),
    ]

    nome_evento = models.CharField(max_length=255)
    descrizione = models.TextField(blank=True)
    artista = models.CharField(max_length=255)
    data_ora = models.DateTimeField()
    luogo = models.CharField(max_length=255)
    citta = models.CharField(max_length=100)
    url_immagine = models.URLField(blank=True)
    categoria = models.CharField(max_length=50, choices=CATEGORIE)
    stato_disponibilita = models.CharField(max_length=20, choices=STATO_DISPONIBILITA, default='disponibile')
    attivo = models.BooleanField(default=True)
    timestamp_aggiornamento = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.nome_evento} - {self.data_ora.strftime('%d/%m/%Y')}"

    class Meta:
        ordering = ['-data_ora']

#modello piattaforma

class Piattaforma(models.Model):
    nome = models.CharField(max_length=100, unique=True)
    url_base = models.URLField()

    def __str__(self):
        return self.nome

    class Meta:
        verbose_name_plural = "Piattaforme"

# modello eventopiattaforma

class EventoPiattaforma(models.Model):
    STATO_BIGLIETTI = [
        ('disponibile', 'Disponibile'),
        ('sold_out', 'Sold Out'),
    ]

    evento = models.ForeignKey('Evento', on_delete=models.CASCADE, related_name='piattaforme_collegate')
    piattaforma = models.ForeignKey('Piattaforma', on_delete=models.CASCADE, related_name='eventi_collegati')
    url_pagina_evento = models.URLField()
    disponibilita_biglietti = models.CharField(max_length=20, choices=STATO_BIGLIETTI, default='disponibile')
    prezzo_minimo = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    timestamp_aggiornamento = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.evento.nome_evento} su {self.piattaforma.nome}"

    class Meta:
        unique_together = ('evento', 'piattaforma')


# modello biglietto
class Biglietto(models.Model):

    nome = models.CharField(max_length=255,blank=True)
    data_caricamento = models.DateTimeField(auto_now_add=True)
    is_valid = models.BooleanField(default=False)
    path_file = models.FileField(upload_to='uploads/%Y/%m/%d/%H')

    def save(self,*args,**kwargs):
        if not self.nome :
            self.nome = self.path_file.name

        pdf_validation(self.path_file)
        self.is_valid=False

        super().save(*args,**kwargs)