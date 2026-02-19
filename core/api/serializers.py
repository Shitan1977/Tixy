# api/serializers.py
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import serializers
from typing import List
from decimal import Decimal
import hashlib
from django.db.models import Avg, Count
from rest_framework.exceptions import ValidationError

from .models import (
    UserProfile, Artista, Luoghi, Categoria, Evento, Performance,TicketSubitem,ListingSubitem ,
    Piattaforma, EventoPiattaforma, PerformancePiattaforma, InventorySnapshot, TicketUpload,
    Sconti, AlertPlan, Abbonamento, Monitoraggio, Notifica, AlertTrigger, EventFollow,
    Biglietto, Listing, ListingTicket, OrderTicket, Payment, Rivendita, Acquisto, Recensione

)
from .utils import invia_otp_email
import os

User = get_user_model()


# ============ USER ============

class UserProfileSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, required=False)
    facebook_url = serializers.URLField(required=False, allow_blank=True, allow_null=True)
    instagram_url = serializers.URLField(required=False, allow_blank=True, allow_null=True)
    tiktok_url = serializers.URLField(required=False, allow_blank=True, allow_null=True)
    x_url = serializers.URLField(required=False, allow_blank=True, allow_null=True)

    class Meta:
        model = User
        read_only_fields = ("id", "created_at", "updated_at", "is_staff")
        fields = (
            "id", "email", "first_name", "last_name",
            "phone_number", "date_of_birth", "gender",
            "country", "city", "address", "zip_code", "document_id",
            "notify_email", "notify_whatsapp", "notify_push",
            "accepted_terms", "accepted_privacy",
            "is_active", "is_staff",
            "created_at", "updated_at",
            "password",
            # Social media
            "facebook_url", "instagram_url", "tiktok_url", "x_url", "marketing_ok",
        )

    def create(self, validated_data):
        password = validated_data.pop("password", None)
        if not password:
            raise serializers.ValidationError({"password": "password required"})
        user = User(**validated_data)
        user.set_password(password)
        user.save()
        return user

    def update(self, instance, validated_data):
        password = validated_data.pop("password", None)
        for attr, val in validated_data.items():
            setattr(instance, attr, val)
        if password:
            instance.set_password(password)
        instance.save()
        return instance


class UserRegistrationSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True)

    class Meta:
        model = User
        fields = ("id", "email", "password", "first_name", "last_name",
                  "accepted_terms", "accepted_privacy")

    def validate(self, attrs):
        if not attrs.get("accepted_terms"):
            raise serializers.ValidationError({"accepted_terms": "terms must be accepted"})
        if not attrs.get("accepted_privacy"):
            raise serializers.ValidationError({"accepted_privacy": "privacy must be accepted"})
        email = attrs.get("email", "").lower().strip()
        if User.objects.filter(email=email).exists():
            raise serializers.ValidationError({"email": "email already registered"})
        attrs["email"] = email
        return attrs

    def create(self, validated_data):
        password = validated_data.pop("password")
        user = User(**validated_data)
        user.set_password(password)
        user.is_active = False  # wait OTP
        user.save()
        user.generate_otp()
        try:
            invia_otp_email(user)
        except Exception:
            # non bloccare la registrazione se l'email fallisce
            pass
        return user


class OTPVerificationSerializer(serializers.Serializer):
    email = serializers.EmailField()
    otp_code = serializers.CharField(max_length=6)

    def validate(self, attrs):
        try:
            user = User.objects.get(email=attrs["email"])
        except User.DoesNotExist:
            raise serializers.ValidationError({"email": "user not found"})
        if not user.is_otp_valid(attrs["otp_code"]):
            raise serializers.ValidationError({"otp_code": "invalid or expired"})
        self.user = user
        return attrs

    def save(self, **kwargs):
        user = getattr(self, "user", None)
        user.is_active = True
        user.otp_code = None
        user.otp_created_at = None
        user.is_verified = True
        user.gdpr_consent_at = user.gdpr_consent_at or timezone.now()
        user.save(update_fields=["is_active", "otp_code", "otp_created_at", "is_verified", "gdpr_consent_at"])
        return {"detail": "account verified"}


class ShortUserProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ("id", "first_name", "last_name")


# ============ CATALOGO / PIATTAFORME ============

class ArtistaSerializer(serializers.ModelSerializer):
    class Meta:
        model = Artista
        fields = "__all__"


class LuoghiSerializer(serializers.ModelSerializer):
    class Meta:
        model = Luoghi
        fields = "__all__"


class CategoriaSerializer(serializers.ModelSerializer):
    class Meta:
        model = Categoria
        fields = "__all__"


class PiattaformaSerializer(serializers.ModelSerializer):
    class Meta:
        model = Piattaforma
        fields = "__all__"


class PerformanceMiniSerializer(serializers.ModelSerializer):
    evento_nome = serializers.CharField(source="evento.nome_evento", read_only=True)
    luogo_nome = serializers.CharField(source="luogo.nome", read_only=True)

    class Meta:
        model = Performance
        fields = (
            "id", "evento", "evento_nome", "luogo", "luogo_nome",
            "starts_at_utc", "status",
            "disponibilita_agg", "prezzo_min", "prezzo_max", "valuta"
        )
        read_only_fields = ("evento_nome", "luogo_nome")


class EventoPiattaformaSerializer(serializers.ModelSerializer):
    piattaforma = PiattaformaSerializer(read_only=True)
    piattaforma_id = serializers.PrimaryKeyRelatedField(
        source="piattaforma", queryset=Piattaforma.objects.all(), write_only=True, required=False
    )

    class Meta:
        model = EventoPiattaforma
        fields = (
            "id", "evento", "piattaforma", "piattaforma_id",
            "id_evento_piattaforma", "url",
            "ultima_scansione", "snapshot_raw", "checksum_dati",
            "creato_il", "aggiornato_il"
        )


class EventoSerializer(serializers.ModelSerializer):
    categoria = CategoriaSerializer(read_only=True)
    artista_principale = ArtistaSerializer(read_only=True)
    performances = PerformanceMiniSerializer(many=True, read_only=True)
    mappings_evento = EventoPiattaformaSerializer(many=True, read_only=True)

    class Meta:
        model = Evento
        fields = "__all__"


class PerformancePiattaformaSerializer(serializers.ModelSerializer):
    piattaforma = PiattaformaSerializer(read_only=True)
    piattaforma_id = serializers.PrimaryKeyRelatedField(
        source="piattaforma", queryset=Piattaforma.objects.all(), write_only=True, required=False
    )

    class Meta:
        model = PerformancePiattaforma
        fields = (
            "id", "performance", "piattaforma", "piattaforma_id",
            "external_perf_id", "url", "ultima_scansione",
            "snapshot_raw", "checksum_dati", "creato_il", "aggiornato_il"
        )


class InventorySnapshotSerializer(serializers.ModelSerializer):
    performance_info = PerformanceMiniSerializer(source="performance", read_only=True)
    piattaforma_nome = serializers.CharField(source="piattaforma.nome", read_only=True)

    class Meta:
        model = InventorySnapshot
        fields = (
            "id", "performance", "performance_info", "piattaforma",
            "piattaforma_nome", "taken_at", "availability_status",
            "min_price", "max_price", "currency", "raw_json"
        )
        read_only_fields = ("performance_info", "piattaforma_nome")


# ============ ABBONAMENTI / ALERT ============

class ScontiSerializer(serializers.ModelSerializer):
    class Meta:
        model = Sconti
        fields = "__all__"


class AlertPlanSerializer(serializers.ModelSerializer):
    class Meta:
        model = AlertPlan
        fields = "__all__"


class AbbonamentoSerializer(serializers.ModelSerializer):
    utente = serializers.HiddenField(default=serializers.CurrentUserDefault())
    utente_info = ShortUserProfileSerializer(source="utente", read_only=True)
    plan_info = AlertPlanSerializer(source="plan", read_only=True)
    sconto_info = ScontiSerializer(source="sconto", read_only=True)
    class Meta:
        model = Abbonamento
        fields = "__all__"
        read_only_fields = ("data_inizio", "utente",)  # <-- assicurati che "utente" sia qui


class MonitoraggioSerializer(serializers.ModelSerializer):
    class Meta:
        model = Monitoraggio
        fields = "__all__"

    def validate(self, attrs):
        evento = attrs.get("evento") or getattr(self.instance, "evento", None)
        performance = attrs.get("performance") or getattr(self.instance, "performance", None)
        if not evento and not performance:
            raise serializers.ValidationError("provide event or performance")
        return attrs


class NotificaSerializer(serializers.ModelSerializer):
    class Meta:
        model = Notifica
        fields = "__all__"
        read_only_fields = ("sent_at",)


class AlertTriggerSerializer(serializers.ModelSerializer):
    class Meta:
        model = AlertTrigger
        fields = "__all__"


class EventFollowSerializer(serializers.ModelSerializer):
    class Meta:
        model = EventFollow
        fields = "__all__"
        read_only_fields = ["user", "created_at"]


# ============ RECENSIONI ============


class RecensioneSerializer(serializers.ModelSerializer):
    venditore_info = ShortUserProfileSerializer(source="venditore", read_only=True)
    acquirente_info = ShortUserProfileSerializer(source="acquirente", read_only=True)

    # 👇 campo con messaggi custom
    order = serializers.PrimaryKeyRelatedField(
        queryset=OrderTicket.objects.all(),
        required=False,
        allow_null=True,
        error_messages={
            "does_not_exist": "numero d'ordine non corrispondente",
            "incorrect_type": "numero d'ordine non valido",
            "required": "inserisci il numero d'ordine",
            "null": "numero d'ordine non valido",
        },
    )

    class Meta:
        model = Recensione
        fields = "__all__"

    def validate(self, attrs):
        order = attrs.get("order")
        venditore = attrs.get("venditore")
        acquirente = attrs.get("acquirente") or getattr(self.instance, "acquirente", None)

        # auto-set dell’acquirente dal request se mancante
        if not acquirente:
            req = self.context.get("request")
            if req and req.user and req.user.is_authenticated:
                attrs["acquirente"] = req.user
                acquirente = req.user

        # range rating
        if "rating" in attrs:
            r = attrs["rating"]
            if r < 1 or r > 5:
                raise serializers.ValidationError({"rating": "rating must be 1..5"})

        # se è stato inserito un ordine, deve combaciare con utente e venditore
        if order:
            if order.buyer_id != (acquirente.id if acquirente else None):
                raise serializers.ValidationError({"order": "numero d'ordine non corrispondente"})
            if venditore and order.listing.seller_id != venditore.id:
                raise serializers.ValidationError({"order": "numero d'ordine non corrispondente"})
            # (opzionale) consenti solo ordini conclusi:
            # if order.status not in ("PAID", "DELIVERED"):
            #     raise serializers.ValidationError({"order": "ordine non completato"})

        return attrs



# ============ BIGLIETTI / MARKETPLACE ============

class BigliettoUploadSerializer(serializers.ModelSerializer):
    path_file = serializers.FileField(max_length=None, allow_empty_file=False)

    class Meta:
        model = Biglietto
        fields = "__all__"
        extra_kwargs = {
            "path_file": {"required": True, "allow_null": False}
        }

    def validate_path_file(self, file):
        max_size = 2 * 1024 * 1024  # 2MB
        if file.size > max_size:
            raise serializers.ValidationError("file too large (max 2MB)")
        ext = os.path.splitext(file.name)[1].lower()
        if ext != ".pdf":
            raise serializers.ValidationError("file must be PDF")
        return file

    def to_representation(self, instance):
        rep = super().to_representation(instance)
        request = self.context.get("request")
        if instance.path_file and request:
            rep["path_file"] = request.build_absolute_uri(instance.path_file.url)
        return rep


class ListingTicketSerializer(serializers.ModelSerializer):
    class Meta:
        model = ListingTicket
        fields = "__all__"




class OrderTicketSerializer(serializers.ModelSerializer):
    buyer_info = ShortUserProfileSerializer(source="buyer", read_only=True)

    class Meta:
        model = OrderTicket
        fields = "__all__"
        read_only_fields = ("created_at", "paid_at", "delivered_at")

    def validate(self, attrs):
        listing = attrs.get("listing")
        qty = attrs.get("qty", 1)
        unit_price = attrs.get("unit_price")

        if qty <= 0:
            raise serializers.ValidationError({"qty": "must be > 0"})
        if unit_price is None or unit_price <= 0:
            raise serializers.ValidationError({"unit_price": "must be > 0"})
        if listing and qty > listing.qty:
            raise serializers.ValidationError({"qty": "exceeds listing qty"})
        # total consistency (client may send total_price)
        attrs["total_price"] = unit_price * qty
        attrs.setdefault("currency", listing.currency if listing else "EUR")
        return attrs


class PaymentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Payment
        fields = "__all__"



class ListingCardSerializer(serializers.ModelSerializer):
    performance_info = PerformanceMiniSerializer(source="performance", read_only=True)
    seller_info = ShortUserProfileSerializer(source="seller", read_only=True)
    seller_reviews_count = serializers.IntegerField(read_only=True)
    seller_rating_avg = serializers.DecimalField(max_digits=3, decimal_places=2, read_only=True)
    total_price = serializers.SerializerMethodField()

    class Meta:
        model = Listing
        fields = (
            "id",
            "seller", "seller_info",
            "performance", "performance_info",
            "seat_category", "section", "row", "seat_from", "seat_to",
            "qty", "price_each", "currency", "delivery_method",
            "status", "expires_at", "notes",
            "created_at", "updated_at",
            "seller_reviews_count", "seller_rating_avg",
            "total_price",
            "is_top",  # <-- QUI
        )
        read_only_fields = (
            "created_at", "updated_at",
            "seller_reviews_count", "seller_rating_avg", "total_price",
            "is_top",  # se vuoi tenerlo read-only in questa scheda
        )

    def get_total_price(self, obj):
        try:
            return (obj.price_each or 0) * (obj.qty or 0)
        except Exception:
            return None




class RivenditaSerializer(serializers.ModelSerializer):
    class Meta:
        model = Rivendita
        fields = "__all__"


class AcquistoSerializer(serializers.ModelSerializer):
    class Meta:
        model = Acquisto
        fields = "__all__"

# ---- CHECKOUT SERIALIZERS (non-model) ----

class CheckoutStartSerializer(serializers.Serializer):
    listing = serializers.PrimaryKeyRelatedField(queryset=Listing.objects.all())
    qty = serializers.IntegerField(min_value=1)
    # buyer fields (anche per guest)
    email = serializers.EmailField()
    first_name = serializers.CharField(max_length=100)
    last_name = serializers.CharField(max_length=100)
    phone_number = serializers.CharField(max_length=20, required=False, allow_blank=True)
    # opzionale: crea account
    create_account = serializers.BooleanField(default=False)
    password = serializers.CharField(write_only=True, required=False, allow_blank=False)
    accepted_terms = serializers.BooleanField()
    accepted_privacy = serializers.BooleanField()
    # fee opzionali per UI (commissioni/spese gestione)
    fee_percent = serializers.DecimalField(max_digits=5, decimal_places=2, required=False)
    fee_flat = serializers.DecimalField(max_digits=10, decimal_places=2, required=False)

    def validate(self, attrs):
        listing: Listing = attrs["listing"]
        qty = attrs["qty"]
        if listing.status != "ACTIVE":
            raise serializers.ValidationError({"listing": "listing not active"})
        if qty > listing.qty:
            raise serializers.ValidationError({"qty": f"exceeds listing qty ({listing.qty} available)"})
        if not attrs.get("accepted_terms"):
            raise serializers.ValidationError({"accepted_terms": "terms must be accepted"})
        if not attrs.get("accepted_privacy"):
            raise serializers.ValidationError({"accepted_privacy": "privacy must be accepted"})
        if attrs.get("create_account") and not attrs.get("password"):
            raise serializers.ValidationError({"password": "required when create_account is true"})
        return attrs


class OrderSummarySerializer(serializers.ModelSerializer):
    buyer_info = ShortUserProfileSerializer(source="buyer", read_only=True)
    listing_info = ListingCardSerializer(source="listing", read_only=True)
    # breakdown calcolato (commissioni/spese)
    subtotal = serializers.CharField(read_only=True)
    commission = serializers.CharField(read_only=True)
    total = serializers.CharField(read_only=True)

    class Meta:
        model = OrderTicket
        fields = (
            "id", "status", "currency",
            "buyer", "buyer_info",
            "listing", "listing_info",
            "qty", "unit_price", "total_price",
            "subtotal", "commission", "total",
            "created_at",
        )
        read_only_fields = (
            "status", "buyer", "buyer_info",
            "unit_price", "total_price", "currency",
            "created_at", "subtotal", "commission", "total",
        )
class PerformanceRelatedSerializer(serializers.ModelSerializer):
    evento_nome = serializers.CharField(source="evento.nome_evento", read_only=True)
    luogo_nome = serializers.CharField(source="luogo.nome", read_only=True)
    listings_count = serializers.IntegerField(read_only=True)
    best_listing_price = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True, allow_null=True)

    class Meta:
        model = Performance
        fields = (
            "id",
            "evento", "evento_nome",
            "luogo", "luogo_nome",
            "starts_at_utc",
            "status",
            "disponibilita_agg", "prezzo_min", "prezzo_max", "valuta",
            "listings_count", "best_listing_price",
        )



class MonitoraggioListSerializer(MonitoraggioSerializer):
    evento_info = EventoSerializer(source="evento", read_only=True)
    performance_info = PerformanceMiniSerializer(source="performance", read_only=True)
    period_label = serializers.SerializerMethodField()
    expires_at = serializers.SerializerMethodField()

    class Meta(MonitoraggioSerializer.Meta):
        # ATTENZIONE: quando il base usa "__all__", qui lasciamo "__all__"
        # I campi dichiarati sopra (SerializerMethodField, *info) vengono inclusi automaticamente.
        fields = "__all__"

    def get_period_label(self, obj):
        if getattr(obj, "durata_giorni", None):
            return f"{obj.durata_giorni} giorni"
        if hasattr(obj, "data_inizio") and hasattr(obj, "data_fine") and obj.data_inizio and obj.data_fine:
            try:
                return f"{(obj.data_fine - obj.data_inizio).days} giorni"
            except Exception:
                pass
        return "—"

    def get_expires_at(self, obj):
        if getattr(obj, "data_fine", None):
            return obj.data_fine
        if hasattr(obj, "data_inizio") and getattr(obj, "durata_giorni", None):
            try:
                return obj.data_inizio + timedelta(days=obj.durata_giorni)
            except Exception:
                pass
        return None


class EventFollowListSerializer(EventFollowSerializer):
    evento_info = EventoSerializer(source="event", read_only=True)

    class Meta(EventFollowSerializer.Meta):
        # Stesso motivo: il base ha "__all__", quindi lasciamo "__all__"
        fields = "__all__"

# --- Elenco abbonamenti PRO per evento (item “piatto” per la UI) ---

class ProSubscriptionItemSerializer(serializers.Serializer):
    """
    Rappresenta un 'abbonamento PRO per evento' (tipicamente un Monitoraggio legato ad Abbonamento PRO).
    Campi:
      - id: id del Monitoraggio
      - event_title: titolo evento
      - event_date: data fine/inizio evento (usa Performance.starts_at_utc se presente)
      - activated_at: data attivazione abbonamento PRO (Abbonamento.data_inizio)
      - expires_at: scadenza dell’alert PRO (Abbonamento.expires_at o calcolo da plan.periodo_mesi)
      - status: 'active' | 'expired' | 'pending' | 'closed'
      - status_label: 'Attivo' | 'Scaduto' | 'Pedding' | 'Chiuso'
    """
    id = serializers.IntegerField()
    event_id = serializers.IntegerField(allow_null=True)
    event_title = serializers.CharField()
    event_date = serializers.DateTimeField(allow_null=True)
    activated_at = serializers.DateTimeField(allow_null=True)
    expires_at = serializers.DateTimeField(allow_null=True)
    status = serializers.CharField()
    status_label = serializers.CharField()

    def to_representation(self, obj):
        # obj atteso: Monitoraggio (con .abbonamento, .evento/.performance)
        now = timezone.now()

        # Evento / titolo
        ev = getattr(obj, "evento", None)
        perf = getattr(obj, "performance", None)
        if perf and getattr(perf, "evento", None):
            ev = perf.evento
        title = getattr(ev, "nome_evento", None) or getattr(obj, "query", None) or "Evento"

        # Data evento: preferiamo la performance se c’è, altrimenti prima performance dell’evento
        event_date = getattr(perf, "starts_at_utc", None)
        if not event_date and ev:
            try:
                # related_name tipico: performances (o performance_set)
                qs = getattr(ev, "performances", None)
                if qs is not None:
                    first_perf = qs.order_by("starts_at_utc").first()
                    event_date = getattr(first_perf, "starts_at_utc", None)
                else:
                    first_perf = ev.performance_set.order_by("starts_at_utc").first()
                    event_date = getattr(first_perf, "starts_at_utc", None)
            except Exception:
                pass

        # Abbonamento / PRO
        ab = getattr(obj, "abbonamento", None)
        activated_at = getattr(ab, "data_inizio", None)

        # Scadenza PRO: usa campo diretto se esiste; altrimenti calcolo da plan.periodo_mesi (~30gg/mes)
        # Scadenza PRO:
        # 1) campo diretto (se esiste)
        # 2) data_fine (se presente)
        # 3) calcolo da plan.periodo_mesi
        # 4) fallback: 30 giorni da activated_at
        expires = getattr(ab, "expires_at", None) if ab else None

        if not expires and ab:
            expires = getattr(ab, "data_fine", None)

        if not expires:
            try:
                mesi = getattr(getattr(ab, "plan", None), "periodo_mesi", None) if ab else None
                if mesi and activated_at:
                    expires = activated_at + timedelta(days=30 * int(mesi))
            except Exception:
                pass

        if not expires and activated_at:
            expires = activated_at + timedelta(days=30)

        # Verifica se ha inviato almeno una notifica (email/alert)
        has_sent_alerts = False
        try:
            has_sent_alerts = obj.notifiche.filter(status="SENT").exists()
        except Exception:
            pass

        # Stato (nuova logica)
        # 1. Chiuso: evento passato (data evento superata)
        # 2. Scaduto: abbonamento scaduto MA evento ancora in programmazione
        # 3. Attivo: ha inviato almeno una notifica/email
        # 4. Pending: abbonamento attivo ma non ha ancora trovato biglietti
        
        if event_date and event_date < now:
            # Evento già passato -> CHIUSO
            status = "closed"
        elif expires and expires < now:
            # Abbonamento scaduto (ma evento non ancora passato) -> SCADUTO
            status = "expired"
        elif has_sent_alerts:
            # Ha inviato almeno una notifica -> ATTIVO
            status = "active"
        elif getattr(ab, "attivo", False):
            # Abbonamento attivo ma non ha ancora trovato biglietti -> PENDING
            status = "pending"
        else:
            # Altri casi (es. abbonamento non attivo) -> PENDING
            status = "pending"

        labels = {
            "active": "Attivo",
            "expired": "Scaduto",
            "pending": "Pending",
            "closed": "Chiuso",
        }

        # Estrai event_id
        event_id_value = None
        if hasattr(obj, "evento_id") and obj.evento_id:
            event_id_value = obj.evento_id
        elif hasattr(obj, "evento") and obj.evento and hasattr(obj.evento, "id"):
            event_id_value = obj.evento.id
        elif perf and hasattr(perf, "evento_id") and perf.evento_id:
            event_id_value = perf.evento_id
        elif perf and hasattr(perf, "evento") and perf.evento and hasattr(perf.evento, "id"):
            event_id_value = perf.evento.id

        return {
            "id": getattr(obj, "id", None),
            "event_id": event_id_value,
            "event_title": title,
            "event_date": event_date,
            "activated_at": activated_at,
            "expires_at": expires,
            "status": status,
            "status_label": labels.get(status, status.title()),
        }
class MyPurchasesItemSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    listing_id = serializers.IntegerField()
    event_title = serializers.CharField()
    venue = serializers.CharField(allow_blank=True)
    performance_datetime = serializers.DateTimeField()
    qty = serializers.IntegerField()
    price_total = serializers.CharField()
    currency = serializers.CharField()
    status = serializers.CharField()
    download_api_url = serializers.CharField()
# --- 1A) Helper: calcolo hash per dedup file uguale ---
def sha256_of_file(inmem_file) -> str:
    """
    Calcola sha256 dell'UploadedFile senza perdere il cursore.
    """
    pos = inmem_file.tell()
    inmem_file.seek(0)
    h = hashlib.sha256()
    for chunk in iter(lambda: inmem_file.read(8192), b""):
        h.update(chunk)
    inmem_file.seek(pos)
    return h.hexdigest()
# --- 1B) Upload PDF ---
class TicketUploadPDFSerializer(serializers.Serializer):
    path_file = serializers.FileField()
    performance = serializers.PrimaryKeyRelatedField(queryset=Performance.objects.all())

    def validate_path_file(self, f):
        ext = (f.name or "").lower()
        if not ext.endswith(".pdf"):
            raise serializers.ValidationError("file must be PDF")
        if f.size > 15 * 1024 * 1024:
            raise serializers.ValidationError("file too large (max 15MB)")
        return f

    def create(self, validated_data):
        request = self.context["request"]
        user = request.user
        f = validated_data["path_file"]
        perf = validated_data.get("performance")

        big = Biglietto.objects.create(path_file=f, nome_file=f.name, is_valid=False)
        upload = TicketUpload.objects.create(seller=user, biglietto=big)

        # Avvia parsing async (o sync fallback)
        from .tasks import parse_ticket_pdf
        parse_ticket_pdf.delay(upload.id)

        return {"upload_id": upload.id}

    def save(self, **kwargs):
        return self.create(self.validated_data)
# --- 1C) Upload URL (senza file) ---
class TicketUploadURLSerializer(serializers.Serializer):
    url = serializers.URLField()
    performance = serializers.PrimaryKeyRelatedField(queryset=Performance.objects.all(), required=False, allow_null=True)

    def create(self, validated_data):
        request = self.context["request"]
        user = request.user
        url = validated_data["url"]

        # crea un Biglietto "vuoto" (verrà popolato dal task che scarica il PDF)
        big = Biglietto.objects.create(is_valid=False)
        upload = TicketUpload.objects.create(seller=user, biglietto=big, source_url=url)

        from .tasks import parse_ticket_pdf
        parse_ticket_pdf.delay(upload.id)

        return {"upload_id": upload.id}

    def save(self, **kwargs):
        return self.create(self.validated_data)
class TicketSubitemMiniSerializer(serializers.ModelSerializer):
    class Meta:
        model = TicketSubitem
        fields = ("id", "full_name", "price", "page", "code_type", "is_listed", "is_sold")

# --- 2A) Subitem (pezzo singolo vendibile) ---
class TicketSubitemSerializer(serializers.ModelSerializer):
    class Meta:
        model = TicketSubitem
        fields = (
            "id", "full_name", "code_type", "code_hash",
            "settore", "fila", "posto", "price", "page",
            "is_listed", "is_sold", "listed_at", "sold_at",
        )
        read_only_fields = ("is_listed", "is_sold", "listed_at", "sold_at")
# --- 2B) Dettaglio Biglietto dopo parsing ---
class BigliettoDetailSerializer(serializers.ModelSerializer):
    performance_info = PerformanceMiniSerializer(source="performance", read_only=True)
    subitems = TicketSubitemSerializer(source="subitems", many=True, read_only=True)

    class Meta:
        model = Biglietto
        fields = (
            "id", "nome_file", "nome_intestatario",
            "qr_code", "sigillo_fiscale", "hash_file",
            "mime_type", "file_size",
            "pages_count", "tickets_found",
            "extracted_names", "extracted_prices", "extracted_meta",
            "evento", "performance", "performance_info",
            "is_valid", "creato_il", "aggiornato_il",
            "subitems",
        )
# --- 2C) Review dell'UPLOAD (aggregato) ---
class TicketUploadReviewSerializer(serializers.ModelSerializer):
    biglietto_info = serializers.SerializerMethodField()
    subitems = TicketSubitemMiniSerializer(source="biglietto.subitems", many=True, read_only=True)

    class Meta:
        model = TicketUpload
        fields = ("id", "status", "found_count", "selectable_count", "error_message", "biglietto_info", "subitems")

    def get_biglietto_info(self, obj):
        b = obj.biglietto
        perf = None
        # se vuoi, puoi inferire performance da logica tua (qui non forziamo)
        return {
            "id": b.id,
            "nome_file": b.nome_file,
            "hash_file": b.hash_file,
            "pages_count": b.pages_count,
            "tickets_found": b.tickets_found,
            "extracted_names": b.extracted_names,
            "extracted_prices": b.extracted_prices,
        }
class ListingCreateFromUploadSerializer(serializers.Serializer):
    upload_id = serializers.IntegerField()
    subitem_ids = serializers.ListField(child=serializers.IntegerField(), min_length=1)
    price_each = serializers.DecimalField(max_digits=10, decimal_places=2)
    currency = serializers.CharField(max_length=3, default="EUR")
    delivery_method = serializers.ChoiceField(choices=Listing.DELIVERY, default="PDF")
    change_name_required = serializers.BooleanField(default=False)
    notes = serializers.CharField(allow_blank=True, required=False)
    performance = serializers.PrimaryKeyRelatedField(  # <— AGGIUNGI
        queryset=Performance.objects.all(), required=False, allow_null=True
    )

    def validate(self, attrs):
        upload = get_object_or_404(TicketUpload, pk=attrs["upload_id"])
        request = self.context["request"]
        if not (request.user.is_staff or upload.seller_id == request.user.id):
            raise serializers.ValidationError("not allowed")

        qs = TicketSubitem.objects.filter(
            id__in=attrs["subitem_ids"], biglietto=upload.biglietto, is_listed=False
        )
        if qs.count() != len(attrs["subitem_ids"]):
            raise serializers.ValidationError("some selected tickets are not available")

        # performance: payload > inferita dal biglietto
        perf = attrs.get("performance")
        if perf is None:
            perf = getattr(upload.biglietto, "performance", None)
        if perf is None:
            raise serializers.ValidationError("performance mancante: passala nel payload o associane una al biglietto")

        attrs["_upload"] = upload
        attrs["_subitems_qs"] = qs
        attrs["_performance"] = perf
        return attrs

    def create(self, validated_data):
        upload = validated_data["_upload"]
        sub_qs = validated_data["_subitems_qs"]
        performance = validated_data["_performance"]
        user = self.context["request"].user

        with transaction.atomic():
            listing = Listing.objects.create(
                seller=user,
                performance=performance,
                qty=sub_qs.count(),
                price_each=validated_data["price_each"],
                currency=validated_data["currency"],
                delivery_method=validated_data["delivery_method"],
                notes=validated_data.get("notes") or "",
                status="ACTIVE",
            )
            # collega subitems + marca is_listed
            for sbi in sub_qs.select_for_update():
                ListingSubitem.objects.create(listing=listing, subitem=sbi)
                sbi.is_listed = True
                sbi.listed_at = timezone.now()
                sbi.save(update_fields=["is_listed", "listed_at"])

        return {
            "listing_id": listing.id,
            "qty": listing.qty,
            "price_each": str(listing.price_each),
            "currency": listing.currency,
            "performance_id": listing.performance_id,
            "status": listing.status,
        }

class MyResaleListItemSerializer(serializers.ModelSerializer):
    performance_info = PerformanceMiniSerializer(source="performance", read_only=True)
    seller_info = ShortUserProfileSerializer(source="seller", read_only=True)
    download_url = serializers.SerializerMethodField()
    sold_qty = serializers.SerializerMethodField()
    is_fully_sold = serializers.SerializerMethodField()
    change_name_required = serializers.SerializerMethodField()

    class Meta:
        model = Listing
        fields = (
            "id", "status", "qty", "price_each", "currency", "delivery_method",
            "performance_info", "seller_info",
            "download_url", "sold_qty", "is_fully_sold",
            "change_name_required",     # <-- AGGIUNTO
            "notes", "created_at",
        )

    def get_download_url(self, obj):
        rel = obj.subitems.select_related("subitem__biglietto").first()
        bt = rel.subitem.biglietto if rel and rel.subitem else None
        if bt and getattr(bt, "path_file", None):
            req = self.context.get("request")
            try:
                url = bt.path_file.url
            except Exception:
                return None
            return req.build_absolute_uri(url) if req else url
        return None

    def get_sold_qty(self, obj):
        return obj.subitems.filter(subitem__is_sold=True).count()

    def get_is_fully_sold(self, obj):
        return self.get_sold_qty(obj) >= (obj.qty or 0)

    def get_change_name_required(self, obj):
        """
        Regola: se mancano >= 24h all'evento -> True (cambio nome richiesto).
        Fallback: se non ho data, euristica per PDF/E-TICKET.
        """
        # 1) Provo a leggere la data/ora evento dalla performance
        starts_iso = None
        perf = getattr(obj, "performance", None)
        if perf is not None:
            # adatta i nomi dei campi alla tua Performance
            starts_iso = getattr(perf, "starts_at_utc", None) or getattr(perf, "starts_at", None)

        if starts_iso:
            from datetime import datetime, timedelta, timezone as tz
            try:
                # accetto stringhe ISO con o senza 'Z'
                s = str(starts_iso)
                if isinstance(starts_iso, datetime):
                    dt = starts_iso if starts_iso.tzinfo else starts_iso.replace(tzinfo=tz.utc)
                else:
                    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=tz.utc)
                return (dt - datetime.now(tz.utc)) >= timedelta(hours=24)
            except Exception:
                pass

        # 2) Fallback: euristica
        return obj.delivery_method in ("PDF", "E_TICKET")


class MarkListingSoldSerializer(serializers.Serializer):
    listing_id = serializers.IntegerField()
    subitem_ids = serializers.ListField(child=serializers.IntegerField(), min_length=1)

    def validate(self, attrs):
        request = self.context["request"]
        listing = get_object_or_404(Listing, pk=attrs["listing_id"])
        if not (request.user.is_staff or request.user.id == listing.seller_id):
            raise ValidationError("not allowed")
        # subitem devono essere del listing
        cnt = ListingSubitem.objects.filter(listing=listing, subitem_id__in=attrs["subitem_ids"]).count()
        if cnt != len(attrs["subitem_ids"]):
            raise ValidationError("some subitems do not belong to this listing")
        attrs["_listing"] = listing
        return attrs

    def create(self, validated_data):
        listing = validated_data["_listing"]
        subs = TicketSubitem.objects.filter(id__in=validated_data["subitem_ids"])
        updated = 0
        with transaction.atomic():
            for s in subs.select_for_update():
                if not s.is_sold:
                    s.is_sold = True
                    s.sold_at = timezone.now()
                    s.save(update_fields=["is_sold", "sold_at"])
                    updated += 1
            sold = listing.subitems.filter(subitem__is_sold=True).count()
            if sold >= (listing.qty or 0):
                listing.status = "SOLD"
                listing.save(update_fields=["status"])
        return {"listing_id": listing.id, "sold": updated, "total": listing.qty, "status": listing.status}

class TicketDownloadSerializer(serializers.Serializer):
    listing_id = serializers.IntegerField()

    def create(self, validated_data):
        listing = get_object_or_404(Listing, pk=validated_data["listing_id"])
        bt = listing.tickets.first()
        if not (bt and bt.path_file):
            raise serializers.ValidationError("file non disponibile")
        req = self.context.get("request")
        url = req.build_absolute_uri(bt.path_file.url) if req else bt.path_file.url
        return {"download_url": url}

    def save(self, **kwargs):
        return self.create(self.validated_data)

class ListingSubitemInlineSerializer(serializers.ModelSerializer):
    class Meta:
        model = ListingSubitem
        fields = ("id", "subitem")  # o denormalizza fields utili

# api/serializers.py (aggiunte)
class ListingCreateFromUploadSerializer(serializers.Serializer):
    upload_id = serializers.IntegerField()
    subitem_ids = serializers.ListField(child=serializers.IntegerField(), min_length=1)
    price_each = serializers.DecimalField(max_digits=10, decimal_places=2)
    currency = serializers.CharField(max_length=3, default="EUR")
    delivery_method = serializers.ChoiceField(choices=Listing.DELIVERY, default="PDF")
    change_name_required = serializers.BooleanField(default=False)
    notes = serializers.CharField(allow_blank=True, required=False)
    performance = serializers.PrimaryKeyRelatedField(queryset=Performance.objects.all(), required=True)  # 👈 NEW

    def validate(self, attrs):
        upload = get_object_or_404(TicketUpload, pk=attrs["upload_id"])
        request = self.context["request"]
        if not (request.user.is_staff or upload.seller_id == request.user.id):
            raise serializers.ValidationError("not allowed")

        qs = TicketSubitem.objects.filter(id__in=attrs["subitem_ids"], biglietto=upload.biglietto, is_listed=False)
        if qs.count() != len(attrs["subitem_ids"]):
            raise serializers.ValidationError("some selected tickets are not available")

        attrs["_upload"] = upload
        attrs["_subitems_qs"] = qs
        return attrs

    def create(self, validated_data):
        upload = validated_data["_upload"]
        sub_qs = validated_data["_subitems_qs"]
        user = self.context["request"].user
        performance = validated_data["performance"]  # 👈 usare quella passata

        with transaction.atomic():
            listing = Listing.objects.create(
                seller=user,
                performance=performance,
                qty=sub_qs.count(),
                price_each=validated_data["price_each"],
                currency=validated_data["currency"],
                delivery_method=validated_data["delivery_method"],
                notes=validated_data.get("notes") or "",
                status="ACTIVE",
            )
            for sbi in sub_qs.select_for_update():
                ListingSubitem.objects.create(listing=listing, subitem=sbi)
                sbi.is_listed = True
                sbi.save(update_fields=["is_listed"])

        return {"listing_id": listing.id, "qty": listing.qty, "price_each": str(listing.price_each),
                "currency": listing.currency, "performance_id": listing.performance_id, "status": listing.status}

# api/serializers.py (aggiunte)

from rest_framework import serializers
from .models import SupportTicket, SupportMessage, SupportAttachment, OrderTicket, Listing, Biglietto, TicketUpload

class SupportAttachmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = SupportAttachment
        fields = ["id", "file", "original_name", "uploaded_at"]
        read_only_fields = ["id", "uploaded_at"]

class SupportMessageSerializer(serializers.ModelSerializer):
    author_name = serializers.SerializerMethodField()
    attachments = SupportAttachmentSerializer(many=True, read_only=True)

    class Meta:
        model = SupportMessage
        fields = ["id", "author", "author_name", "body", "created_at", "is_internal", "attachments"]
        read_only_fields = ["id", "author", "author_name", "created_at"]

    def get_author_name(self, obj):
        full = f"{obj.author.first_name} {obj.author.last_name}".strip()
        return full or (obj.author.email or f"User {obj.author_id}")

class SupportTicketCreateSerializer(serializers.ModelSerializer):
    # Primo messaggio (opzionale ma utile per apertura)
    first_message = serializers.CharField(write_only=True, required=False, allow_blank=True)

    class Meta:
        model = SupportTicket
        fields = [
            "id", "title", "category", "priority",
            "order", "listing", "biglietto", "ticket_upload",
            "first_message"
        ]

    def validate(self, data):
        # Richiedi almeno un aggancio
        if not any([data.get("order"), data.get("listing"), data.get("biglietto"), data.get("ticket_upload")]):
            raise serializers.ValidationError("Collega il ticket a un ordine/listing/biglietto/upload.")
        return data

    def create(self, validated):
        user = self.context["request"].user
        first_message = validated.pop("first_message", "").strip()
        ticket = SupportTicket.objects.create(user=user, **validated)
        if first_message:
            SupportMessage.objects.create(ticket=ticket, author=user, body=first_message, is_internal=False)
        return ticket

class SupportTicketListItemSerializer(serializers.ModelSerializer):
    last_update = serializers.DateTimeField(source="updated_at", read_only=True)
    messages_count = serializers.IntegerField(read_only=True)
    class Meta:
        model = SupportTicket
        fields = ["id", "title", "status", "priority", "category", "order", "listing", "biglietto", "ticket_upload", "last_update", "messages_count"]

class SupportTicketDetailSerializer(serializers.ModelSerializer):
    messages = SupportMessageSerializer(many=True, read_only=True)
    class Meta:
        model = SupportTicket
        fields = [
            "id", "title", "status", "priority", "category",
            "order", "listing", "biglietto", "ticket_upload",
            "created_at", "updated_at", "assigned_to", "messages"
        ]
        read_only_fields = ["id", "created_at", "updated_at", "assigned_to", "messages"]
class SupportAddMessageSerializer(serializers.Serializer):
    body = serializers.CharField()
    is_internal = serializers.BooleanField(required=False, default=False)

    def create(self, validated_data):
        # usiamo context["ticket"] e context["request"].user
        ticket = self.context["ticket"]
        user = self.context["request"].user
        # Se l'utente NON è staff, forziamo is_internal=False
        is_internal = validated_data.get("is_internal", False) if user.is_staff else False
        return SupportMessage.objects.create(ticket=ticket, author=user, body=validated_data["body"], is_internal=is_internal)

class SupportAddAttachmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = SupportAttachment
        fields = ["id", "file", "original_name"]
        read_only_fields = ["id"]

    def create(self, validated_data):
        message = self.context["message"]
        return SupportAttachment.objects.create(message=message, **validated_data)
