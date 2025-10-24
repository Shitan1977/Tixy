from datetime import datetime, timedelta
import os
import re
from django.utils import timezone
from django.db import models
from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin


# =========================
# User
# =========================

class UserProfileManager(BaseUserManager):
    def create_user(self, email, password=None, **extra_fields):
        if not email:
            raise ValueError("email required")
        email = self.normalize_email(email)
        user = self.model(email=email, **extra_fields)
        if password:
            user.set_password(password)
        else:
            user.set_unusable_password()
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        extra_fields.setdefault("is_active", True)
        return self.create_user(email, password, **extra_fields)


class UserProfile(AbstractBaseUser, PermissionsMixin):
    GENDER = [
        ("male", "Maschio"),
        ("female", "Femmina"),
        ("other", "Altro"),
        ("na", "Preferisco non dirlo"),
    ]

    email = models.EmailField(unique=True)
    first_name = models.CharField(max_length=100, verbose_name="Nome")
    last_name  = models.CharField(max_length=100, verbose_name="Cognome")

    otp_code = models.CharField(max_length=6, blank=True, null=True)
    otp_created_at = models.DateTimeField(blank=True, null=True)

    phone_number = models.CharField(max_length=20, blank=True, null=True, verbose_name="Numero di telefono")
    date_of_birth = models.DateField(blank=True, null=True, verbose_name="Data di nascita")
    gender = models.CharField(max_length=20, choices=GENDER, default="na", verbose_name="Sesso")

    country = models.CharField(max_length=2, blank=True, null=True, verbose_name="Nazione")  # ISO-3166 alpha2
    city = models.CharField(max_length=100, blank=True, null=True, verbose_name="Città")
    address = models.CharField(max_length=255, blank=True, null=True, verbose_name="Indirizzo")
    zip_code = models.CharField(max_length=20, blank=True, null=True)
    document_id = models.CharField(max_length=50, blank=True, null=True, verbose_name="Codice documento", help_text="Codice del documento d'identità")

    notify_email = models.BooleanField(default=True, verbose_name="Notifiche email")
    notify_whatsapp = models.BooleanField(default=False,verbose_name="Notifiche wahtsapp")
    notify_push = models.BooleanField(default=True,verbose_name="Notifiche push")

    accepted_terms = models.BooleanField(default=False, verbose_name="Termini accettati")
    accepted_privacy = models.BooleanField(default=False, verbose_name="Privacy accettata")
    gdpr_consent_at = models.DateTimeField(blank=True, null=True, verbose_name="Data accettazione GDPR")

    is_verified = models.BooleanField(default=False)
    is_active  = models.BooleanField(default=True)
    is_staff   = models.BooleanField(default=False)

    deleted_at = models.DateTimeField(blank=True, null=True, verbose_name="Data eliminazione")
    last_login = models.DateTimeField(blank=True, null=True, verbose_name="Data ultimo accesso")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Data creazione")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Data ultimo aggiornamento")

    objects = UserProfileManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["first_name", "last_name"]

    def __str__(self):
        return f"{self.first_name} {self.last_name} ({self.email})"

    # OTP helpers
    def generate_otp(self):
        import random
        self.otp_code = str(random.randint(100000, 999999))
        self.otp_created_at = timezone.now()
        self.save(update_fields=["otp_code", "otp_created_at"])
        return self.otp_code

    def is_otp_valid(self, code):
        if not self.otp_code or not self.otp_created_at:
            return False
        if self.otp_code != code:
            return False
        return timezone.now() <= self.otp_created_at + timedelta(minutes=10)

    class Meta:
        verbose_name="Utente"
        verbose_name_plural="Utenti"
        indexes = [
            models.Index(fields=["email"]),
            models.Index(fields=["document_id"]),
        ]


# =========================
# Catalogo: Artista / Luogo / Categoria / Evento / Performance
# =========================

class Artista(models.Model):
    TIPO = [
        ("artista", "Artista"),
        ("squadra", "Squadra"),
        ("atleta", "Atleta"),
        ("altro", "Altro"),
    ]
    nome = models.CharField(max_length=255, blank=True, null=True)
    nome_normalizzato = models.CharField(max_length=255, unique=True, blank=True, null=True)
    tipo = models.CharField(max_length=7, choices=TIPO, default="artista")
    nomi_alternativi = models.JSONField(blank=True, null=True)
    creato_il = models.DateTimeField(auto_now_add=True)
    aggiornato_il = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.nome or f"artista:{self.pk}"

    class Meta:
        verbose_name="Artista"
        verbose_name_plural="Artisti"
        indexes = [
            models.Index(fields=["nome"]),
            models.Index(fields=["nome_normalizzato"]),
        ]


class Luoghi(models.Model):
    nome = models.CharField(max_length=255, default="")
    nome_normalizzato = models.CharField(max_length=255, default="")
    indirizzo = models.CharField(max_length=255, blank=True, null=True)
    citta = models.CharField(max_length=120, blank=True, null=True)
    citta_normalizzata = models.CharField(max_length=120, blank=True, null=True)
    stato_iso = models.CharField(max_length=2, blank=True, null=True)
    # opzionali utili
    lat = models.DecimalField(max_digits=9, decimal_places=6, blank=True, null=True)
    lng = models.DecimalField(max_digits=9, decimal_places=6, blank=True, null=True)
    timezone = models.CharField(max_length=64, blank=True, null=True)

    creato_il = models.DateTimeField(auto_now_add=True)
    aggiornato_il = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.nome

    class Meta:
        verbose_name="Luogo"
        verbose_name_plural="Luoghi"
        indexes = [
            models.Index(fields=["nome_normalizzato"]),
            models.Index(fields=["citta_normalizzata"]),
        ]
        constraints = [
            models.UniqueConstraint(fields=["nome_normalizzato"], name="uq_luoghi_nome_norm")
        ]


class Categoria(models.Model):
    slug = models.CharField(max_length=60, unique=True, default="")
    nome = models.CharField(max_length=120, default="")

    def __str__(self):
        return self.nome

    class Meta:
        verbose_name="Categoria"
        verbose_name_plural="Categorie"
        indexes = [models.Index(fields=["nome"])]


class Evento(models.Model):
    STATO = [
        ("pianificato", "Pianificato"),
        ("annullato", "Annullato"),
        ("rinviato", "Rinviato"),
        ("riprogrammato", "Riprogrammato"),
    ]

    slug = models.CharField(max_length=255, unique=True, default="")
    nome_evento = models.CharField(max_length=255, default="")
    nome_evento_normalizzato = models.CharField(max_length=255, default="")
    descrizione = models.TextField(blank=True, null=True)
    stato = models.CharField(max_length=13, choices=STATO, default="pianificato")
    genere = models.CharField(max_length=120, blank=True, null=True)
    lingua = models.CharField(max_length=40, blank=True, null=True)
    immagine_url = models.CharField(max_length=512, blank=True, null=True)

    artista_principale = models.ForeignKey(
        Artista, on_delete=models.SET_NULL, blank=True, null=True, related_name="eventi_principali"
    )
    categoria = models.ForeignKey(
        Categoria, on_delete=models.SET_NULL, blank=True, null=True, related_name="eventi"
    )

    hash_canonico = models.CharField(max_length=64, unique=True, default="")
    note_raw = models.JSONField(blank=True, null=True)
    creato_il = models.DateTimeField(auto_now_add=True)
    aggiornato_il = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.nome_evento

    class Meta:
        verbose_name="Evento"
        verbose_name_plural="Eventi"
        indexes = [
            models.Index(fields=["nome_evento_normalizzato"]),
            models.Index(fields=["stato"]),
        ]


class Performance(models.Model):
    STATUS = [
        ("ONSALE", "OnSale"),
        ("SOLD_OUT", "SoldOut"),
        ("POSTPONED", "Postponed"),
        ("CANCELLED", "Cancelled"),
        ("ENDED", "Ended"),
    ]

    DISP = [
        ("disponibile", "Disponibile"),
        ("limitata", "Limitata"),
        ("non_disponibile", "Non Disponibile"),
        ("sconosciuta", "Sconosciuta"),
    ]

    evento = models.ForeignKey(Evento, on_delete=models.CASCADE, related_name="performances")
    luogo = models.ForeignKey(Luoghi, on_delete=models.CASCADE, related_name="performances")
    starts_at_utc = models.DateTimeField()
    ends_at_utc = models.DateTimeField(blank=True, null=True)
    doors_at_utc = models.DateTimeField(blank=True, null=True)

    status = models.CharField(max_length=12, choices=STATUS, default="ONSALE")
    disponibilita_agg = models.CharField(max_length=16, choices=DISP, default="sconosciuta")

    prezzo_min = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True)
    prezzo_max = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True)
    valuta = models.CharField(max_length=3, blank=True, null=True)

    creato_il = models.DateTimeField(auto_now_add=True)
    aggiornato_il = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.evento.nome_evento} @ {self.luogo.nome} {self.starts_at_utc}"

    class Meta:
        verbose_name="Performance"
        verbose_name_plural="Performances"
        ordering = ["-starts_at_utc"]
        indexes = [
            models.Index(fields=["evento", "starts_at_utc"]),
            models.Index(fields=["luogo", "starts_at_utc"]),
            models.Index(fields=["status"]),
        ]


# =========================
# Piattaforme, Mapping, Snapshot
# =========================

class Piattaforma(models.Model):
    nome = models.CharField(max_length=60, unique=True, default="")
    dominio = models.CharField(max_length=120, blank=True, null=True)
    attivo = models.BooleanField(default=True)

    def __str__(self):
        return self.nome

    class Meta:
        verbose_name="Piattaforma"
        verbose_name_plural="Piattaforme"


class EventoPiattaforma(models.Model):
    # mapping livello EVENTO
    evento = models.ForeignKey(Evento, on_delete=models.CASCADE, related_name="mappings_evento")
    piattaforma = models.ForeignKey(Piattaforma, on_delete=models.CASCADE, related_name="mappings_evento")
    id_evento_piattaforma = models.CharField(max_length=255, blank=True, null=True)
    url = models.CharField(max_length=1024, default="")
    ultima_scansione = models.DateTimeField()
    snapshot_raw = models.JSONField(blank=True, null=True)
    checksum_dati = models.CharField(max_length=64, blank=True, null=True)
    creato_il = models.DateTimeField(auto_now_add=True)
    aggiornato_il = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.evento} @ {self.piattaforma}"

    class Meta:
        verbose_name="Evento Piattaforma"
        verbose_name_plural="Evento Piattaforme"
        constraints = [

            models.UniqueConstraint(
                fields=["piattaforma", "id_evento_piattaforma"],
                name="uq_evento_plat_external",
                condition=~models.Q(id_evento_piattaforma=None),
            ),
            # evita duplicati evento-piattaforma senza id esterno
            models.UniqueConstraint(fields=["evento", "piattaforma"], name="uq_evento_plat_pair"),
        ]
        indexes = [models.Index(fields=["ultima_scansione"])]


class PerformancePiattaforma(models.Model):
    # mapping livello PERFORMANCE (replica)
    performance = models.ForeignKey(Performance, on_delete=models.CASCADE, related_name="mappings")
    piattaforma = models.ForeignKey(Piattaforma, on_delete=models.CASCADE, related_name="mappings_performance")
    external_perf_id = models.CharField(max_length=255)
    url = models.CharField(max_length=1024, default="")
    ultima_scansione = models.DateTimeField()
    snapshot_raw = models.JSONField(blank=True, null=True)
    checksum_dati = models.CharField(max_length=64, blank=True, null=True)
    creato_il = models.DateTimeField(auto_now_add=True)
    aggiornato_il = models.DateTimeField(auto_now=True)

    #def __str__(self):
     #   return f"{self.performance} @ {self.piattaforma}"

    class Meta:
        verbose_name="Performance Piattaforma"
        verbose_name_plural="Performance Piattaforme"
        constraints = [
            models.UniqueConstraint(fields=["piattaforma", "external_perf_id"], name="uq_perf_plat_external"),
        ]
        indexes = [
            models.Index(fields=["performance"]),
            models.Index(fields=["ultima_scansione"]),
        ]


class InventorySnapshot(models.Model):
    AVAIL = [
        ("AVAILABLE", "Available"),
        ("LIMITED", "Limited"),
        ("SOLD_OUT", "SoldOut"),
        ("UNKNOWN", "Unknown"),
    ]
    performance = models.ForeignKey(Performance, on_delete=models.CASCADE, related_name="snapshots")
    piattaforma = models.ForeignKey(Piattaforma, on_delete=models.CASCADE, related_name="snapshots")
    taken_at = models.DateTimeField()
    availability_status = models.CharField(max_length=10, choices=AVAIL, default="UNKNOWN")
    min_price = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True)
    max_price = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True)
    currency = models.CharField(max_length=3, blank=True, null=True)
    raw_json = models.JSONField(blank=True, null=True)

    class Meta:
        verbose_name="InventorySnapshot"
        verbose_name_plural="InventorySnapshots"
        indexes = [
            models.Index(fields=["performance", "piattaforma", "taken_at"]),
        ]


# =========================
# Abbonamenti / Alert
# =========================

class Sconti(models.Model):
    durata_mesi = models.IntegerField()
    percentuale = models.IntegerField()
    descrizione = models.TextField(blank=True, null=True)

    def __str__(self):
        return f"sconto {self.percentuale}% per {self.durata_mesi} mesi"
    
    class Meta:
        verbose_name="Sconto"
        verbose_name_plural="Sconti"


class AlertPlan(models.Model):
    name = models.CharField(max_length=80)
    duration_days = models.IntegerField()
    price = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(max_length=3, default="EUR")
    max_events = models.IntegerField(default=50)
    max_push_per_day = models.IntegerField(default=10)

    def __str__(self):
        return self.name
    
    class Meta:
        verbose_name="Piano Alert"
        verbose_name_plural="Piani Alert"


class Abbonamento(models.Model):
    utente = models.ForeignKey(UserProfile, on_delete=models.CASCADE, related_name="abbonamenti")
    sconto = models.ForeignKey(Sconti, on_delete=models.SET_NULL, null=True, blank=True, related_name="abbonamenti")
    plan = models.ForeignKey(AlertPlan, on_delete=models.SET_NULL, null=True, blank=True, related_name="abbonamenti", verbose_name="Piano Alert")
    prezzo = models.DecimalField(max_digits=10, decimal_places=2, default=0.0)
    data_inizio = models.DateTimeField(auto_now_add=True)
    data_fine = models.DateTimeField(blank=True, null=True)
    attivo = models.BooleanField(default=True)

    def __str__(self):
        return f"abbonamento {self.id} utente {self.utente_id}"
    
    class Meta:
        verbose_name="Abbonamento"
        verbose_name_plural="Abbonamenti"

class Monitoraggio(models.Model):
    # watch per evento o performance
    abbonamento = models.ForeignKey(Abbonamento, on_delete=models.CASCADE, related_name="monitoraggi")
    evento = models.ForeignKey(Evento, on_delete=models.CASCADE, related_name="monitoraggi", blank=True, null=True)
    performance = models.ForeignKey(Performance, on_delete=models.CASCADE, related_name="monitoraggi", blank=True, null=True)
    filters_json = models.JSONField(blank=True, null=True)  # es: price_cap, platforms, settore
    creato_il = models.DateTimeField(auto_now_add=True)
    aggiornato_il = models.DateTimeField(auto_now=True)

    def __str__(self):
        target = self.performance_id or self.evento_id
        return f"monitoraggio {self.id} target {target}"

    class Meta:
        verbose_name="Monitoraggio"
        verbose_name_plural="Monitoraggi"
        constraints = [
            models.CheckConstraint(
                check=(models.Q(evento__isnull=False) | models.Q(performance__isnull=False)),
                name="ck_monitoraggio_target",
            )
        ]


class Notifica(models.Model):
    CHANNEL = [("email", "Email"), ("push", "Push"), ("whatsapp", "WhatsApp"), ("sms", "SMS")]
    STATUS = [("SENT", "Sent"), ("FAILED", "Failed")]

    monitoraggio = models.ForeignKey(Monitoraggio, on_delete=models.CASCADE, related_name="notifiche")
    channel = models.CharField(max_length=10, choices=CHANNEL, default="push")
    dedupe_key = models.CharField(max_length=120, blank=True, null=True)
    sent_at = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=8, choices=STATUS, default="SENT")
    message = models.TextField()

    def __str__(self):
        return f"notifica {self.id} {self.channel} {self.status}"

    class Meta:
        verbose_name="Notifica"
        verbose_name_plural="Notifiche"
        indexes = [
            models.Index(fields=["monitoraggio"]),
            models.Index(fields=["dedupe_key"]),
        ]


class AlertTrigger(models.Model):
    REASON = [("BACK_IN_STOCK", "BackInStock"), ("PRICE_DROP", "PriceDrop"), ("NEW_DATE", "NewDate")]
    monitoraggio = models.ForeignKey(Monitoraggio, on_delete=models.CASCADE, related_name="triggers")
    snapshot = models.ForeignKey(InventorySnapshot, on_delete=models.SET_NULL, blank=True, null=True, related_name="triggers")
    triggered_at = models.DateTimeField(auto_now_add=True)
    reason = models.CharField(max_length=20, choices=REASON)

    class Meta:
        verbose_name="Trigger Alert"
        verbose_name_plural="Triggers Alert"
        indexes = [models.Index(fields=["triggered_at"])]


class EventFollow(models.Model):
    user = models.ForeignKey(UserProfile, on_delete=models.CASCADE, related_name="event_follows", verbose_name="Utente")
    event = models.ForeignKey(Evento, on_delete=models.CASCADE, related_name="followers", verbose_name="Evento")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name="Evento Seguito"
        verbose_name_plural="Eventi Seguiti"
        constraints = [
            models.UniqueConstraint(fields=["user", "event"], name="uq_event_follow")
        ]


# =========================
# Recensioni
# =========================

class Recensione(models.Model):
    testo = models.TextField()
    rating = models.PositiveSmallIntegerField(default=5)  # 1..5

    venditore = models.ForeignKey(UserProfile, on_delete=models.CASCADE, related_name="recensioni_ricevute")
    acquirente = models.ForeignKey(UserProfile, on_delete=models.SET_NULL, blank=True, null=True, related_name="recensioni_scritte")
    order = models.ForeignKey("OrderTicket", on_delete=models.SET_NULL, blank=True, null=True, related_name="recensioni")

    creato_il = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"review {self.id} seller {self.venditore_id}"

    class Meta:
        verbose_name="Recensione"
        verbose_name_plural="Recensioni"
        constraints = [
            models.UniqueConstraint(fields=["order", "acquirente"], name="uq_review_per_order_author")
        ]
        indexes = [models.Index(fields=["venditore"]), models.Index(fields=["acquirente"])]


# =========================
# Biglietti e Marketplace
# =========================

def biglietto_path(instance, filename):
    return f"uploads/{datetime.now().strftime('%Y/%m')}/{filename}"


class Biglietto(models.Model):
    nome_file = models.CharField(max_length=255, blank=True, null=True)
    nome_intestatario = models.CharField(max_length=255, blank=True, null=True)
    sigillo_fiscale = models.CharField(max_length=16, blank=True, null=True)
    path_file = models.FileField(upload_to=biglietto_path)
    hash_file = models.CharField(max_length=64, blank=True, null=True)
    mime_type = models.CharField(max_length=100, blank=True, null=True)
    file_size = models.IntegerField(blank=True, null=True)

    is_valid = models.BooleanField(default=False)
    creato_il = models.DateTimeField(auto_now_add=True)
    aggiornato_il = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.nome_file} ({self.nome_intestatario})" or f"ticket:{self.pk}"

    def save(self, *args, **kwargs):
        if not self.nome_file and self.path_file:
            raw_name = os.path.basename(self.path_file.name)
            safe_name = re.sub(r"[^a-zA-Z0-9._-]", "_", raw_name)
            self.nome_file = safe_name
            self.is_valid = False
        super().save(*args, **kwargs)

    class Meta:
        verbose_name="Biglietto"
        verbose_name_plural="Biglietti"
        constraints = [
            models.UniqueConstraint(fields=["hash_file"], name="uq_ticket_hash", condition=~models.Q(hash_file=None))
        ]


class Listing(models.Model):
    DELIVERY = [("E_TICKET", "E_TICKET"), ("PDF", "PDF"), ("COURIER", "COURIER"), ("APP_TRANSFER", "APP_TRANSFER")]
    STATUS = [("ACTIVE", "ACTIVE"), ("RESERVED", "RESERVED"), ("SOLD", "SOLD"), ("CANCELLED", "CANCELLED"), ("EXPIRED", "EXPIRED")]

    seller = models.ForeignKey(UserProfile, on_delete=models.CASCADE, related_name="listings")
    # meglio legare alla performance (data/luogo)
    performance = models.ForeignKey(Performance, on_delete=models.CASCADE, related_name="listings")

    seat_category = models.CharField(max_length=80, blank=True, null=True)
    section = models.CharField(max_length=80, blank=True, null=True)
    row = models.CharField(max_length=40, blank=True, null=True)
    seat_from = models.IntegerField(blank=True, null=True)
    seat_to = models.IntegerField(blank=True, null=True)

    qty = models.IntegerField(default=1)
    price_each = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(max_length=3, default="EUR")
    delivery_method = models.CharField(max_length=12, choices=DELIVERY, default="PDF")

    status = models.CharField(max_length=10, choices=STATUS, default="ACTIVE")
    expires_at = models.DateTimeField(blank=True, null=True)
    notes = models.TextField(blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # opzionale: collegare file specifici
    tickets = models.ManyToManyField(Biglietto, through="ListingTicket", related_name="listings")

    def __str__(self):
        return f"listing {self.id} perf {self.performance_id} seller {self.seller_id}"

    class Meta:
        verbose_name="Lista"
        verbose_name_plural="Liste"
        indexes = [
            models.Index(fields=["performance", "status"]),
            models.Index(fields=["seller"]),
        ]


class ListingTicket(models.Model):
    listing = models.ForeignKey(Listing, on_delete=models.CASCADE)
    biglietto = models.ForeignKey(Biglietto, on_delete=models.CASCADE)

    def __str__(self):
        return f"{self.listing.id} - {self.listing.seller} @ {self.biglietto}"

    class Meta:
        verbose_name="Lista Biglietto"
        verbose_name_plural="Liste Biglietti"
        constraints = [
            models.UniqueConstraint(fields=["listing", "biglietto"], name="uq_listing_ticket")
        ]


class OrderTicket(models.Model):
    STATUS = [("PENDING", "PENDING"), ("PAID", "PAID"), ("DELIVERED", "DELIVERED"), ("REFUNDED", "REFUNDED"), ("CANCELLED", "CANCELLED")]

    buyer = models.ForeignKey(UserProfile, on_delete=models.CASCADE, related_name="orders")
    listing = models.ForeignKey(Listing, on_delete=models.PROTECT, related_name="orders")

    qty = models.IntegerField(default=1)
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)
    total_price = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(max_length=3, default="EUR")

    status = models.CharField(max_length=10, choices=STATUS, default="PENDING")
    created_at = models.DateTimeField(auto_now_add=True)
    paid_at = models.DateTimeField(blank=True, null=True)
    delivered_at = models.DateTimeField(blank=True, null=True)

    def __str__(self):
        return f"order {self.id} listing {self.listing_id} buyer {self.buyer_id}"

    class Meta:
        verbose_name="Ordine Biglietto"
        verbose_name_plural="Ordini Biglietti"
        indexes = [
            models.Index(fields=["buyer", "status"]),
            models.Index(fields=["created_at"]),
        ]


class Payment(models.Model):
    PROVIDER = [("STRIPE", "STRIPE"), ("PAYPAL", "PAYPAL"), ("OTHER", "OTHER")]
    STATUS = [("REQUIRES_ACTION", "REQUIRES_ACTION"), ("SUCCEEDED", "SUCCEEDED"), ("FAILED", "FAILED")]

    order = models.ForeignKey(OrderTicket, on_delete=models.CASCADE, related_name="payments")
    provider = models.CharField(max_length=12, choices=PROVIDER, default="STRIPE")
    provider_intent_id = models.CharField(max_length=120, blank=True, null=True)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(max_length=3, default="EUR")
    status = models.CharField(max_length=16, choices=STATUS, default="REQUIRES_ACTION")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name="Pagamento"
        verbose_name_plural="Pagamenti"


class Rivendita(models.Model):
    evento = models.ForeignKey(Evento, on_delete=models.CASCADE, related_name="rivendite")
    venditore = models.ForeignKey(UserProfile, null=True, on_delete=models.SET_NULL, related_name="rivendite_venditore")
    biglietto = models.ForeignKey(Biglietto, on_delete=models.CASCADE, related_name="rivendite_biglietto")
    url = models.CharField(max_length=1024, blank=True, null=True)
    prezzo = models.DecimalField(max_digits=10, decimal_places=2, default=0.0)
    disponibile = models.BooleanField(default=True)
    creato_il = models.DateTimeField(auto_now_add=True)
    aggiornato_il = models.DateTimeField(auto_now=True)

    def save(self, *args, **kwargs):
        if not self.url and self.biglietto and self.biglietto.path_file:
            self.url = self.biglietto.path_file.url
        super().save(*args, **kwargs)

    def __str__(self):
        return f"rivendita {self.id} evento {self.evento_id}"
    
    class Meta:
        verbose_name="Rivendita"
        verbose_name_plural="Rivendite"

class Acquisto(models.Model):
    rivendita = models.ForeignKey(Rivendita, on_delete=models.CASCADE, related_name="acquisti")
    acquirente = models.ForeignKey(UserProfile, on_delete=models.CASCADE, related_name="acquisti")
    data_acquisto = models.DateTimeField(auto_now_add=True)
    stato = models.CharField(max_length=12, choices=[("in_corso","in_corso"), ("completato","completato"), ("rimborsato","rimborsato")], default="in_corso")
    creato_il = models.DateTimeField(auto_now_add=True)
    aggiornato_il = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"acquisto {self.id} rivendita {self.rivendita_id}"
    
    class Meta:
        verbose_name="Acquisto"
        verbose_name_plural="Acquisti"
