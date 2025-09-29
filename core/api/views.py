# api/views.py
from decimal import Decimal
from uuid import uuid4
from datetime import datetime

from django.contrib.auth import get_user_model
from django.core.files.storage import default_storage
from django.db import transaction
from django.db.models import Q, Count, Avg, Min
from django.db.models.functions import Lower
from django.shortcuts import get_object_or_404
from django.utils.text import get_valid_filename
import django.utils.timezone as dj_timezone  # usare questo, NON il timezone del modulo datetime

from drf_yasg.utils import swagger_auto_schema
from django_filters.rest_framework import DjangoFilterBackend

from rest_framework import viewsets, permissions, status, generics, filters, mixins
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from . import serializers as s  # per riferirci a serializer aggiuntivi (es. PerformanceRelatedSerializer)
from .serializers import (
    UserProfileSerializer, ShortUserProfileSerializer, UserRegistrationSerializer, OTPVerificationSerializer,
    RecensioneSerializer, ArtistaSerializer, LuoghiSerializer, CategoriaSerializer,
    PiattaformaSerializer, EventoPiattaformaSerializer,
    EventoSerializer, PerformanceMiniSerializer,
    ScontiSerializer, AbbonamentoSerializer, MonitoraggioSerializer, NotificaSerializer,
    BigliettoUploadSerializer, RivenditaSerializer, AcquistoSerializer, ListingCardSerializer, OrderTicketSerializer,
    OrderSummarySerializer, CheckoutStartSerializer,
)
from .models import (
    UserProfile, Artista, Luoghi, Categoria, Evento, Performance,
    Piattaforma, EventoPiattaforma, Sconti, Abbonamento, Monitoraggio,
    Notifica, Biglietto, Rivendita, Acquisto, Listing, OrderTicket,
)
from .filters import PerformanceSearchFilter, EventSearchFilter
from .validation import file_validation

User = get_user_model()


# ---------------------------
# Mixin per evitare errori durante la generazione dello schema Swagger
# ---------------------------

class SwaggerSafeQuerysetMixin:
    """
    Evita errori quando drf-yasg genera lo schema e self.request.user è AnonymousUser.
    Se la view è 'fake' per Swagger, restituiamo un queryset vuoto.
    """
    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            # Ritorna un queryset vuoto della stessa classe per mantenere compatibilità
            base_qs = getattr(super(), "get_queryset", lambda: getattr(self, "queryset", None))()
            if base_qs is not None:
                return base_qs.none()
            # fallback: se non esiste super().get_queryset e abbiamo self.queryset
            if hasattr(self, "queryset") and self.queryset is not None:
                return self.queryset.none()
        # default: lascia alla superclasse
        return super().get_queryset() if hasattr(super(), "get_queryset") else getattr(self, "queryset", None)


# ---------------------------
# Permessi di base
# ---------------------------

class IsAdminOrReadOnly(permissions.BasePermission):
    """SAFE methods per tutti, modifica solo admin."""
    def has_permission(self, request, view):
        if request.method in permissions.SAFE_METHODS:
            return True
        return bool(request.user and request.user.is_staff)


class IsAdminOrIsSelf(permissions.BasePermission):
    """Per risorse utente: o admin, o il proprietario dell'oggetto."""
    def has_object_permission(self, request, view, obj):
        return bool(request.user and (request.user.is_staff or obj == request.user))


# ---------------------------
# USER / AUTH
# ---------------------------

class UserProfileViewSet(SwaggerSafeQuerysetMixin, viewsets.ModelViewSet):
    """
    Admin: CRUD su tutti gli utenti.
    Utente normale: può solo leggere/aggiornare se stesso (via /me/).
    """
    queryset = User.objects.all()
    serializer_class = UserProfileSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        qs = super().get_queryset()
        if getattr(self, "swagger_fake_view", False):
            return qs  # sarà .none() grazie al mixin
        if self.request.user.is_staff:
            return qs
        return qs.filter(pk=self.request.user.pk)

    def get_permissions(self):
        if self.action in ["list", "destroy", "create"]:
            return [permissions.IsAdminUser()]
        return [permissions.IsAuthenticated(), IsAdminOrIsSelf()]

    @action(detail=False, methods=['get'], permission_classes=[permissions.IsAuthenticated])
    def me(self, request):
        serializer = self.get_serializer(request.user)
        return Response(serializer.data)

    @action(detail=False, methods=['delete'], permission_classes=[permissions.IsAuthenticated])
    def deactivate(self, request):
        user = request.user
        user.is_active = False
        user.save(update_fields=["is_active"])
        return Response({"status": "account disattivato"}, status=status.HTTP_204_NO_CONTENT)


class UserProfileAPIView(APIView):
    permission_classes = [IsAuthenticated]
    def get(self, request):
        serializer = UserProfileSerializer(request.user)
        return Response(serializer.data)


class UserRegistrationView(generics.CreateAPIView):
    queryset = User.objects.all()
    serializer_class = UserRegistrationSerializer
    permission_classes = [permissions.AllowAny]


class PublicUserDetailView(generics.RetrieveAPIView):
    queryset = User.objects.all()
    serializer_class = ShortUserProfileSerializer
    permission_classes = [permissions.AllowAny]


class ConfirmOTPView(APIView):
    @swagger_auto_schema(
        request_body=OTPVerificationSerializer,
        operation_summary="Conferma registrazione OTP",
        operation_description="Inserisci email e codice OTP ricevuto via email per completare la registrazione."
    )
    def post(self, request):
        serializer = OTPVerificationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        payload = serializer.save()  # attiva account, pulisce OTP
        user = User.objects.get(email=request.data["email"])
        request.session["user_id"] = user.id
        return Response(payload, status=200)


# ---------------------------
# CATALOGO (CRUD read-only pubblico)
# ---------------------------

class ArtistaViewSet(viewsets.ModelViewSet):
    queryset = Artista.objects.all()
    serializer_class = ArtistaSerializer
    permission_classes = [IsAdminOrReadOnly]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter]
    search_fields = ["nome", "nome_normalizzato"]


class LuoghiViewSet(viewsets.ModelViewSet):
    queryset = Luoghi.objects.all()
    serializer_class = LuoghiSerializer
    permission_classes = [IsAdminOrReadOnly]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter]
    search_fields = ["nome", "citta"]


class CategoriaViewSet(viewsets.ModelViewSet):
    queryset = Categoria.objects.all()
    serializer_class = CategoriaSerializer
    permission_classes = [IsAdminOrReadOnly]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter]
    search_fields = ["slug", "nome"]


class PiattaformaViewSet(viewsets.ModelViewSet):
    queryset = Piattaforma.objects.all()
    serializer_class = PiattaformaSerializer
    permission_classes = [IsAdminOrReadOnly]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter]
    search_fields = ["nome", "dominio"]


class EventoPiattaformaViewSet(viewsets.ModelViewSet):
    queryset = EventoPiattaforma.objects.select_related("evento", "piattaforma").all()
    serializer_class = EventoPiattaformaSerializer
    permission_classes = [IsAdminOrReadOnly]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter]
    search_fields = ["id_evento_piattaforma", "evento__nome_evento", "piattaforma__nome"]


# ---------------------------
# EVENTO (niente campi di Performance qui)
# ---------------------------

class EventoViewSet(viewsets.ModelViewSet):
    queryset = Evento.objects.select_related("artista_principale", "categoria").all()
    serializer_class = EventoSerializer
    permission_classes = [IsAdminOrReadOnly]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['nome_evento', 'artista_principale__nome']
    ordering_fields = ['aggiornato_il']
    filterset_fields = ['categoria', 'artista_principale', 'stato']

    @action(detail=True, methods=['get'])
    def rivendite(self, request, pk=None):
        evento = self.get_object()
        rivendite = Rivendita.objects.filter(evento=evento, disponibile=True)
        serializer = RivenditaSerializer(rivendite, many=True)
        return Response(serializer.data)


# ---------------------------
# MOTORE DI RICERCA (Performance) + Autocomplete
# ---------------------------

class PerformanceSearchViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    """
    GET /api/search/performances/?q=&date_from=&date_to=&city=&category=&availability=&platform=&ordering=
    ordering: starts_at_utc, -starts_at_utc, prezzo_min, prezzo_max
    """
    permission_classes = [permissions.AllowAny]
    serializer_class = PerformanceMiniSerializer
    filterset_class = PerformanceSearchFilter
    ordering_fields = ["starts_at_utc", "prezzo_min", "prezzo_max"]
    ordering = ["starts_at_utc"]

    queryset = (
        Performance.objects
        .select_related("evento", "luogo", "evento__artista_principale", "evento__categoria")
        .all()
    )


@api_view(["GET"])
@permission_classes([permissions.AllowAny])
def autocomplete(request):
    """
    GET /api/autocomplete/?type=artist|event|city&q=...&limit=10
    """
    t = request.query_params.get("type", "event")
    q = (request.query_params.get("q") or "").strip()
    limit = int(request.query_params.get("limit", 10))
    if not q:
        return Response([])

    if t == "artist":
        qs = Artista.objects.filter(nome__icontains=q).order_by(Lower("nome"))[:limit]
        data = [{"id": a.id, "label": a.nome, "type": "artist"} for a in qs]

    elif t == "city":
        qs = (
            Luoghi.objects
            .filter(Q(citta__icontains=q) | Q(nome__icontains=q))
            .exclude(citta=None)
            .values("citta")
            .distinct()
        )[:limit]
        data = [{"label": r["citta"], "type": "city"} for r in qs if r["citta"]]

    else:  # event
        qs = Evento.objects.filter(
            Q(nome_evento__icontains=q) | Q(artista_principale__nome__icontains=q)
        ).order_by(Lower("nome_evento"))[:limit]
        data = [{"id": e.id, "label": e.nome_evento, "type": "event"} for e in qs]

    return Response(data)


# ---------------------------
# UPLOAD BIGLIETTI
# ---------------------------

class BigliettoUploadView(SwaggerSafeQuerysetMixin, viewsets.ModelViewSet):
    queryset = Biglietto.objects.all()
    serializer_class = BigliettoUploadSerializer
    parser_classes = [MultiPartParser, FormParser]
    permission_classes = [permissions.IsAuthenticated]

    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['nome_file']
    ordering_fields = ['creato_il']
    filterset_fields = ['is_valid', 'creato_il']

    def salvataggio_temporaneo(self, file_caricato):
        temp_dir = "temp_uploads/"
        nome_base = get_valid_filename(file_caricato.name)
        nome_temp = f"{temp_dir}{uuid4().hex}_{nome_base}"
        default_storage.save(nome_temp, file_caricato)
        return nome_temp

    def path_finale(self, file_originale):
        nome = get_valid_filename(file_originale)
        nome_finale = f"uploads/{datetime.now().strftime('%Y/%m')}/{nome}"
        nome_finale = default_storage.get_available_name(nome_finale)
        return nome_finale

    def create(self, request, *args, **kwargs):
        upload = request.FILES.get('path_file')
        if not upload:
            return Response({'error': 'nessun file caricato'}, status=status.HTTP_400_BAD_REQUEST)

        nome_temp = None
        try:
            nome_temp = self.salvataggio_temporaneo(upload)
            with default_storage.open(nome_temp, 'rb') as file:
                sigilli, hash_file = file_validation(file)

            if not sigilli:
                default_storage.delete(nome_temp)
                return Response({'error': 'nessun dato trovato'}, status=status.HTTP_400_BAD_REQUEST)

            if Biglietto.objects.filter(hash_file=hash_file).exists():
                default_storage.delete(nome_temp)
                return Response({'error': 'file duplicato'}, status=status.HTTP_400_BAD_REQUEST)

            nome_finale = self.path_finale(upload.name)
            biglietti = []

            with transaction.atomic():
                primo_b = Biglietto.objects.create(
                    path_file=nome_finale,
                    nome_file=upload.name,
                    sigillo_fiscale=sigilli[0],
                    hash_file=hash_file,
                    is_valid=False
                )
                biglietti.append(primo_b)

                for sigillo in sigilli[1:]:
                    b = Biglietto.objects.create(
                        path_file=nome_finale,
                        nome_file=upload.name,
                        sigillo_fiscale=sigillo,
                        hash_file=hash_file,
                        is_valid=False
                    )
                    biglietti.append(b)

                def fine_processo():
                    try:
                        with default_storage.open(nome_temp, 'rb') as temp_file:
                            default_storage.save(nome_finale, temp_file)
                    finally:
                        if default_storage.exists(nome_temp):
                            default_storage.delete(nome_temp)

                transaction.on_commit(fine_processo)

            serializer = self.get_serializer(biglietti, many=True, context={'request': request})
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        except Exception as e:
            if nome_temp and default_storage.exists(nome_temp):
                default_storage.delete(nome_temp)
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)


# ---------------------------
# ABBONAMENTI / MONITORAGGIO / NOTIFICHE
# ---------------------------

class ScontiViewSet(viewsets.ModelViewSet):
    queryset = Sconti.objects.all()
    serializer_class = ScontiSerializer
    permission_classes = [IsAdminOrReadOnly]


class AbbonamentoViewSet(SwaggerSafeQuerysetMixin, viewsets.ModelViewSet):
    queryset = Abbonamento.objects.select_related("utente", "plan", "sconto").all()
    serializer_class = AbbonamentoSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        qs = super().get_queryset()
        if getattr(self, "swagger_fake_view", False):
            return qs
        if self.request.user.is_staff:
            return qs
        return qs.filter(utente=self.request.user)


class MonitoraggioViewSet(SwaggerSafeQuerysetMixin, viewsets.ModelViewSet):
    queryset = Monitoraggio.objects.select_related("abbonamento", "evento", "performance").all()
    serializer_class = MonitoraggioSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        qs = super().get_queryset()
        if getattr(self, "swagger_fake_view", False):
            return qs
        if self.request.user.is_staff:
            return qs
        return qs.filter(abbonamento__utente=self.request.user)


class NotificaViewSet(SwaggerSafeQuerysetMixin, viewsets.ReadOnlyModelViewSet):
    queryset = Notifica.objects.select_related("monitoraggio").all()
    serializer_class = NotificaSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        qs = super().get_queryset()
        if getattr(self, "swagger_fake_view", False):
            return qs
        if self.request.user.is_staff:
            return qs
        return qs.filter(monitoraggio__abbonamento__utente=self.request.user)


# ---------------------------
# MARKETPLACE legacy
# ---------------------------

class RivenditaViewSet(viewsets.ModelViewSet):
    queryset = Rivendita.objects.select_related("evento", "venditore", "biglietto").all()
    serializer_class = RivenditaSerializer
    permission_classes = [IsAdminOrReadOnly]


class AcquistoViewSet(SwaggerSafeQuerysetMixin, viewsets.ModelViewSet):
    queryset = Acquisto.objects.select_related("rivendita", "acquirente").all()
    serializer_class = AcquistoSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        qs = super().get_queryset()
        if getattr(self, "swagger_fake_view", False):
            return qs
        if self.request.user.is_staff:
            return qs
        return qs.filter(acquirente=self.request.user)

    def perform_create(self, serializer):
        rivendita = serializer.validated_data['rivendita']
        if not rivendita.disponibile:
            raise s.serializers.ValidationError('questo biglietto non è più disponibile.')
        with transaction.atomic():
            rivendita.disponibile = False
            rivendita.save(update_fields=["disponibile"])
            serializer.save(acquirente=self.request.user, stato='completato')


# ---------------------------
# PERFORMANCE (date) + Listings per data + Altre date artista
# ---------------------------

class PerformanceViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Readonly delle performance (date).
    Fornisce anche:
      - /api/performances/{id}/listings/
      - /api/performances/{id}/other_dates/
    """
    permission_classes = [permissions.AllowAny]
    serializer_class = PerformanceMiniSerializer
    queryset = (
        Performance.objects
        .select_related("evento", "luogo", "evento__artista_principale", "evento__categoria")
        .all()
    )
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ["evento", "luogo", "status", "disponibilita_agg"]
    ordering_fields = ["starts_at_utc", "prezzo_min", "prezzo_max"]
    ordering = ["starts_at_utc"]

    @action(detail=True, methods=["get"], permission_classes=[permissions.AllowAny])
    def listings(self, request, pk=None):
        """
        GET /api/performances/{id}/listings/
        Ritorna i listing ATTIVI per questa performance, ordinati per prezzo.
        Include rating medio venditore e numero recensioni.
        """
        perf = self.get_object()
        now = dj_timezone.now()

        qs = (
            Listing.objects
            .select_related("seller", "performance", "performance__evento", "performance__luogo")
            .filter(performance=perf, status="ACTIVE")
            .annotate(
                seller_reviews_count=Count("seller__recensioni_ricevute", distinct=True),
                seller_rating_avg=Avg("seller__recensioni_ricevute__rating"),
            )
            .order_by("price_each", "id")
        )
        qs = qs.filter(Q(expires_at__isnull=True) | Q(expires_at__gt=now))

        page = self.paginate_queryset(qs)
        ser = ListingCardSerializer(page or qs, many=True)
        return self.get_paginated_response(ser.data) if page is not None else Response(ser.data)

    @action(detail=True, methods=["get"], permission_classes=[permissions.AllowAny])
    def other_dates(self, request, pk=None):
        """
        GET /api/performances/{id}/other_dates/
        Elenca le prossime performance dello stesso artista, esclusa la corrente.
        Include: listings_count (attivi) e best_listing_price (min price attivo).
        """
        perf = self.get_object()
        artist_id = perf.evento.artista_principale_id

        qs = (
            Performance.objects
            .select_related("evento", "luogo")
            .filter(evento__artista_principale_id=artist_id, starts_at_utc__gte=dj_timezone.now())
            .exclude(id=perf.id)
            .annotate(
                listings_count=Count("listings", filter=Q(listings__status="ACTIVE")),
                best_listing_price=Min("listings__price_each", filter=Q(listings__status="ACTIVE")),
            )
            .order_by("starts_at_utc")
        )

        page = self.paginate_queryset(qs)
        ser = s.PerformanceRelatedSerializer(page or qs, many=True)
        return self.get_paginated_response(ser.data) if page is not None else Response(ser.data)


# ---------------------------
# LISTINGS (scheda venditore) + preview carrello
# ---------------------------

class ListingViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Readonly: lista/dettaglio dei listing.
    Serve alla UI per la scheda del venditore (dettaglio) e la preview del totale.
    """
    permission_classes = [permissions.AllowAny]
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ["performance", "status", "delivery_method"]
    ordering_fields = ["price_each", "created_at"]
    ordering = ["price_each"]

    def get_queryset(self):
        return (
            Listing.objects
            .select_related("seller", "performance", "performance__evento", "performance__luogo")
            .annotate(
                seller_reviews_count=Count("seller__recensioni_ricevute", distinct=True),
                seller_rating_avg=Avg("seller__recensioni_ricevute__rating"),
            )
        )

    def get_serializer_class(self):
        return ListingCardSerializer

    @action(detail=True, methods=["post"], permission_classes=[permissions.AllowAny])
    def preview(self, request, pk=None):
        """
        POST /api/listings/{id}/preview/
        Body: { "qty": 2, "fee_percent": 10, "fee_flat": 2.5 }  (fee_* opzionali)
        Ritorna breakdown: unit_price, subtotal, commission, total.
        """
        listing = self.get_object()
        try:
            qty = int(request.data.get("qty", 1))
        except (TypeError, ValueError):
            return Response({"qty": "invalid"}, status=status.HTTP_400_BAD_REQUEST)

        if qty < 1:
            return Response({"qty": "must be >= 1"}, status=status.HTTP_400_BAD_REQUEST)
        if listing.status != "ACTIVE":
            return Response({"detail": "listing not active"}, status=status.HTTP_400_BAD_REQUEST)
        if qty > listing.qty:
            return Response({"qty": f"exceeds listing qty ({listing.qty} available)"},
                            status=status.HTTP_400_BAD_REQUEST)

        unit = listing.price_each
        subtotal = unit * qty

        commission = Decimal("0.00")
        try:
            fee_percent = request.data.get("fee_percent", None)
            if fee_percent is not None:
                commission += (subtotal * Decimal(str(fee_percent)) / Decimal("100")).quantize(Decimal("0.01"))
        except Exception:
            pass
        try:
            fee_flat = request.data.get("fee_flat", None)
            if fee_flat is not None:
                commission += Decimal(str(fee_flat)).quantize(Decimal("0.01"))
        except Exception:
            pass

        total = (subtotal + commission).quantize(Decimal("0.01"))

        return Response({
            "listing_id": listing.id,
            "currency": listing.currency,
            "unit_price": str(unit),
            "qty": qty,
            "subtotal": str(subtotal),
            "commission": str(commission),
            "total": str(total),
            "delivery_method": listing.delivery_method,
            "available_qty": listing.qty,
        }, status=status.HTTP_200_OK)


# ---------------------------
# ORDERS (creazione atomica, riserva qty)
# ---------------------------

class OrderTicketViewSet(SwaggerSafeQuerysetMixin, viewsets.ModelViewSet):
    """
    Crea e visualizza ordini. Create richiede auth.
    La create è transazionale: lock del listing, controlli, decremento qty o chiusura listing.
    """
    queryset = OrderTicket.objects.select_related("listing", "buyer").all()
    serializer_class = OrderTicketSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        qs = super().get_queryset()
        if getattr(self, "swagger_fake_view", False):
            return qs
        if self.request.user.is_staff:
            return qs
        return qs.filter(buyer=self.request.user)

    def create(self, request, *args, **kwargs):
        listing_id = request.data.get("listing")
        try:
            qty = int(request.data.get("qty", 1))
        except (TypeError, ValueError):
            return Response({"qty": "invalid"}, status=status.HTTP_400_BAD_REQUEST)

        if qty < 1:
            return Response({"qty": "must be >= 1"}, status=status.HTTP_400_BAD_REQUEST)

        with transaction.atomic():
            listing = get_object_or_404(Listing.objects.select_for_update(), pk=listing_id)

            if listing.status != "ACTIVE":
                return Response({"detail": "listing not active"}, status=status.HTTP_400_BAD_REQUEST)
            if qty > listing.qty:
                return Response({"qty": f"exceeds listing qty ({listing.qty} available)"},
                                status=status.HTTP_400_BAD_REQUEST)

            unit_price = listing.price_each
            currency = listing.currency
            total_price = (unit_price * qty).quantize(Decimal("0.01"))

            order = OrderTicket.objects.create(
                buyer=request.user,
                listing=listing,
                qty=qty,
                unit_price=unit_price,
                total_price=total_price,
                currency=currency,
                status="PENDING",  # poi passerà a PAID dopo il pagamento
            )

            remaining = listing.qty - qty
            if remaining > 0:
                listing.qty = remaining
                listing.save(update_fields=["qty", "updated_at"])
            else:
                listing.qty = 0
                listing.status = "SOLD"
                listing.save(update_fields=["qty", "status", "updated_at"])

        serializer = self.get_serializer(order)
        headers = self.get_success_headers(serializer.data)
        return Response(serializer.data, status=status.HTTP_201_CREATED, headers=headers)


# ---------------------------
# CHECKOUT START + SUMMARY (senza pagamenti per ora)
# ---------------------------

class CheckoutStartView(APIView):
    """
    POST /api/checkout/start/
    Crea (o riusa) l'utente se necessario, crea ordine PENDING atomico,
    ritorna riepilogo (subtotal/commission/total) per la UI.
    """
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        ser = CheckoutStartSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data

        listing: Listing = data["listing"]
        qty: int = data["qty"]

        fee_percent = data.get("fee_percent")
        fee_flat = data.get("fee_flat")

        # 1) identifica/crea utente
        user = request.user if request.user.is_authenticated else None
        if not user:
            user = User.objects.filter(email=data["email"].lower()).first()
            if not user and data.get("create_account"):
                user = User.objects.create_user(
                    email=data["email"].lower(),
                    password=data["password"],
                    first_name=data["first_name"],
                    last_name=data["last_name"],
                    phone_number=data.get("phone_number") or "",
                    accepted_terms=data["accepted_terms"],
                    accepted_privacy=data["accepted_privacy"],
                )
                user.is_active = True
                user.is_verified = False
                user.save(update_fields=["is_active", "is_verified"])
            elif not user:
                user = User.objects.create_user(
                    email=data["email"].lower(),
                    password=None,
                    first_name=data["first_name"],
                    last_name=data["last_name"],
                    phone_number=data.get("phone_number") or "",
                    accepted_terms=data["accepted_terms"],
                    accepted_privacy=data["accepted_privacy"],
                )

        # 2) crea ordine PENDING in modo atomico e scala qty
        with transaction.atomic():
            locked = Listing.objects.select_for_update().get(pk=listing.pk)
            if locked.status != "ACTIVE":
                return Response({"detail": "listing not active"}, status=status.HTTP_400_BAD_REQUEST)
            if qty > locked.qty:
                return Response({"qty": f"exceeds listing qty ({locked.qty} available)"},
                                status=status.HTTP_400_BAD_REQUEST)

            unit_price = locked.price_each
            subtotal = (unit_price * qty)

            commission = Decimal("0.00")
            if fee_percent is not None:
                commission += (subtotal * Decimal(str(fee_percent)) / Decimal("100"))
            if fee_flat is not None:
                commission += Decimal(str(fee_flat))

            commission = commission.quantize(Decimal("0.01"))
            total = (subtotal + commission).quantize(Decimal("0.01"))

            order = OrderTicket.objects.create(
                buyer=user,
                listing=locked,
                qty=qty,
                unit_price=unit_price,
                total_price=subtotal.quantize(Decimal("0.01")),
                currency=locked.currency,
                status="PENDING",
            )

            remaining = locked.qty - qty
            if remaining > 0:
                locked.qty = remaining
                locked.save(update_fields=["qty", "updated_at"])
            else:
                locked.qty = 0
                locked.status = "RESERVED"  # riservato in attesa pagamento
                locked.save(update_fields=["qty", "status", "updated_at"])

        out = OrderSummarySerializer(order).data
        out["subtotal"] = str(subtotal.quantize(Decimal("0.01")))
        out["commission"] = str(commission)
        out["total"] = str(total)
        return Response(out, status=status.HTTP_201_CREATED)


class CheckoutSummaryView(generics.RetrieveAPIView):
    """
    GET /api/checkout/summary/{order_id}/
    Ritorna il riepilogo dell'ordine (serve per step 2 della UI).
    - Se l'utente e' loggato: può vedere solo i propri ordini.
    - Se anonimo: consenti solo se fornisce ?email=... che combacia (uso basilare).
    """
    serializer_class = OrderSummarySerializer
    queryset = OrderTicket.objects.select_related("buyer", "listing", "listing__performance").all()
    permission_classes = [permissions.AllowAny]

    def get_object(self):
        order = get_object_or_404(self.queryset, pk=self.kwargs["pk"])
        user = self.request.user
        if user.is_authenticated and (user.is_staff or order.buyer_id == user.id):
            return order
        email = self.request.queryparams.get("email") if hasattr(self.request, "queryparams") else self.request.query_params.get("email")
        if email and order.buyer.email.lower() == email.lower():
            return order
        raise permissions.PermissionDenied("not allowed")
