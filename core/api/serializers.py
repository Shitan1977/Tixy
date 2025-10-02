# api/serializers.py
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework import serializers
from django.db.models import Avg, Count
from .models import (
    UserProfile, Artista, Luoghi, Categoria, Evento, Performance,
    Piattaforma, EventoPiattaforma, PerformancePiattaforma, InventorySnapshot,
    Sconti, AlertPlan, Abbonamento, Monitoraggio, Notifica, AlertTrigger, EventFollow,
    Biglietto, Listing, ListingTicket, OrderTicket, Payment, Rivendita, Acquisto, Recensione
)
from .utils import invia_otp_email
import os

User = get_user_model()


# ============ USER ============

class UserProfileSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, required=False)

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
    utente_info = ShortUserProfileSerializer(source="utente", read_only=True)
    plan_info = AlertPlanSerializer(source="plan", read_only=True)
    sconto_info = ScontiSerializer(source="sconto", read_only=True)

    class Meta:
        model = Abbonamento
        fields = "__all__"
        read_only_fields = ("data_inizio",)


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


# ============ RECENSIONI ============

class RecensioneSerializer(serializers.ModelSerializer):
    venditore_info = ShortUserProfileSerializer(source="venditore", read_only=True)
    acquirente_info = ShortUserProfileSerializer(source="acquirente", read_only=True)

    class Meta:
        model = Recensione
        fields = "__all__"

    def validate(self, attrs):
        # one review per order/author handled by db constraint; here we add nicer error and auto-fill vendor if possible
        order = attrs.get("order")
        venditore = attrs.get("venditore")
        acquirente = attrs.get("acquirente") or getattr(self.instance, "acquirente", None)

        if order and not venditore:
            # try derive seller from order.listing
            try:
                vend = order.listing.seller
                attrs["venditore"] = vend
            except Exception:
                pass

        if "rating" in attrs:
            r = attrs["rating"]
            if r < 1 or r > 5:
                raise serializers.ValidationError({"rating": "rating must be 1..5"})
        if not acquirente:
            raise serializers.ValidationError({"acquirente": "buyer required"})
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


class ListingSerializer(serializers.ModelSerializer):
    performance_info = PerformanceMiniSerializer(source="performance", read_only=True)
    seller_info = ShortUserProfileSerializer(source="seller", read_only=True)
    tickets = ListingTicketSerializer(source="listingticket_set", many=True, read_only=True)

    class Meta:
        model = Listing
        fields = "__all__"
        read_only_fields = ("created_at", "updated_at",)

    def validate(self, attrs):
        qty = attrs.get("qty", getattr(self.instance, "qty", 1))
        if qty <= 0:
            raise serializers.ValidationError({"qty": "must be > 0"})
        return attrs


class OrderTicketSerializer(serializers.ModelSerializer):
    buyer_info = ShortUserProfileSerializer(source="buyer", read_only=True)
    listing_info = ListingSerializer(source="listing", read_only=True)

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
    seller_rating_avg = serializers.DecimalField(max_digits=3, decimal_places=2, read_only=True)  # es. 4.87
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
        )
        read_only_fields = (
            "created_at", "updated_at",
            "seller_reviews_count", "seller_rating_avg", "total_price",
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