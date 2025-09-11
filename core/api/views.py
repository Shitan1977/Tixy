from drf_yasg.utils import swagger_auto_schema
from rest_framework import viewsets, permissions, status, generics, filters
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.decorators import action
from .serializers import RivenditaSerializer
from .validation import file_validation
from django.contrib.auth import get_user_model
from django.utils.timezone import now
from django.utils.text import get_valid_filename
from django_filters.rest_framework import DjangoFilterBackend
from django.core.files.storage import default_storage
from django.db import transaction
from uuid import uuid4
from datetime import datetime
from rest_framework.permissions import IsAuthenticated
from rest_framework.parsers import MultiPartParser, FormParser
from .models import *
from .serializers import *


User = get_user_model()

# --- USER ---
class IsAdminOrIsSelf(permissions.BasePermission):
    """
    Consente l'accesso all'admin o all'utente stesso.
    """
    def has_object_permission(self, request, view, obj):
        return request.user.is_staff or obj == request.user

class UserProfileViewSet(viewsets.ModelViewSet):
    queryset = User.objects.all()
    serializer_class = UserProfileSerializer
    permission_classes = [IsAdminOrIsSelf]

    @action(detail=False, methods=['get'], permission_classes = [IsAdminOrIsSelf])
    def me(self, request):
        """
        Restituisce i dati del proprio profilo.
        """
        serializer = self.get_serializer(request.user)
        return Response(serializer.data)

    @action(detail=False, methods=['delete'], permission_classes=[IsAdminOrIsSelf])
    def deactivate(self, request):
        """
        Disattiva il proprio profilo.
        """
        user = request.user
        user.is_active = False
        user.save()
        return Response({"status": "Account disattivato"}, status=status.HTTP_204_NO_CONTENT)

class UserProfileAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        serializer = UserProfileSerializer(request.user)
        return Response(serializer.data)


# --- OTP EMAIL ---
class ConfirmOTPView(APIView):
    @swagger_auto_schema(
        request_body=OTPVerificationSerializer,
        operation_summary="Conferma registrazione OTP",
        operation_description="Inserisci email e codice OTP ricevuto via email per completare la registrazione."
    )
    def post(self, request):
        serializer = OTPVerificationSerializer(data=request.data)
        if serializer.is_valid():
            email = serializer.validated_data.get("email")
            user = User.objects.filter(email=email).first()
            if user:
                request.session["user_id"] = user.id  # Salva l'id dell'utente attivato
                return Response(serializer.validated_data, status=200)
            return Response({"error": "Utente non trovato"}, status=404)
        return Response(serializer.errors, status=400)

# --- REGISTRAZIONE PUBBLICA ---
class UserRegistrationView(generics.CreateAPIView):
    queryset = User.objects.all()
    serializer_class = UserRegistrationSerializer
    permission_classes = [permissions.AllowAny]

# --- EVENTO ---
class EventoViewSet(viewsets.ModelViewSet):
    queryset = Evento.objects.all()
    serializer_class = EventoSerializer
    filter_backends = [filters.SearchFilter, filters.OrderingFilter, DjangoFilterBackend]
    search_fields = ['nome_evento', 'artista_principale__nome', 'luogo__nome', 'luogo__citta']
    ordering_fields = ['data_inizio_utc']
    filterset_fields = ['luogo__citta', 'categoria', 'artista_principale', 'disponibilita', 'stato']

    def get_permissions(self):
        if self.request.method in ['GET', 'HEAD', 'OPTIONS']:
            return [permissions.AllowAny()]
        return [IsAdminOrIsSelf()]

    def get_queryset(self):
        qs = super().get_queryset()
        qs = qs.filter(stato='pianificato', data_inizio_utc__gte=now())
        da_data = self.request.query_params.get('da_data')
        a_data = self.request.query_params.get('a_data')
        if da_data:
            qs = qs.filter(data_inizio_utc__gte=da_data)
        if a_data:
            qs = qs.filter(data_inizio_utc__lte=a_data)
        return qs
# --- RIVENDITE DI UN EVENTO ---
    @action(detail=True, methods=['get'])
    def rivendite(self, request, pk=None):
        evento = self.get_object()
        rivendite = Rivendita.objects.filter(evento=evento, disponibile=True)
        serializer = RivenditaSerializer(rivendite, many=True)
        return Response(serializer.data)

# --- UPLOAD BIGLIETTI ---
class BigliettoUploadView(viewsets.ModelViewSet):
    queryset = Biglietto.objects.all()
    serializer_class = BigliettoUploadSerializer
    parser_classes = [MultiPartParser, FormParser]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['nome','data_caricamento']
    ordering_fields = ['data_caricamento']
    filterset_fields = ['nome','data_caricamento']

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
            return Response({'error':'Nessun file caricato'}, status=status.HTTP_400_BAD_REQUEST)

        nome_temp = None
        try:
            nome_temp = self.salvataggio_temporaneo(upload)
            with default_storage.open(nome_temp, 'rb') as file:
                sigilli, hash_file = file_validation(file)
            if not sigilli:
                default_storage.delete(nome_temp)
                return Response({'error':'Nessun dato trovato'}, status=status.HTTP_400_BAD_REQUEST)
            if Biglietto.objects.filter(hash_file=hash_file).exists():
                default_storage.delete(nome_temp)
                return Response({'error': 'File duplicato'}, status=status.HTTP_400_BAD_REQUEST)

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
                            default_storage.save(nome_finale,temp_file)
                    finally:
                        if default_storage.exists(nome_temp):
                            default_storage.delete(nome_temp)

                transaction.on_commit(fine_processo)

            serializer = self.get_serializer(biglietti, many=True, context={'request':request})
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        except Exception as e:
            if nome_temp and default_storage.exists(nome_temp):
                default_storage.delete(nome_temp)
            return  Response({'error':str(e)}, status=status.HTTP_400_BAD_REQUEST)

# --- View abbozzate per gli alti model

    def get(self, request):
        serializer = UserProfileSerializer(request.user)
        return Response(serializer.data)

class RecensioneViewSet(viewsets.ModelViewSet):
    queryset = Recensione.objects.all()
    serializer_class = RecensioneSerializer

class ArtistaViewSet(viewsets.ModelViewSet):
    queryset = Artista.objects.all()
    serializer_class = ArtistaSerializer

class LuoghiViewSet(viewsets.ModelViewSet):
    queryset = Luoghi.objects.all()
    serializer_class = LuoghiSerializer

class CategoriaViewSet(viewsets.ModelViewSet):
    queryset = Categoria.objects.all()
    serializer_class = CategoriaSerializer

class PiattaformaViewSet(viewsets.ModelViewSet):
    queryset = Piattaforma.objects.all()
    serializer_class = PiattaformaSerializer

class EventoPiattaformaViewSet(viewsets.ModelViewSet):
    queryset = EventoPiattaforma.objects.all()
    serializer_class = EventoPiattaformaSerializer

class ScontiViewSet(viewsets.ModelViewSet):
    queryset = Sconti.objects.all()
    serializer_class = ScontiSerializer

class AbbonamentoViewSet(viewsets.ModelViewSet):
    queryset = Abbonamento.objects.all()
    serializer_class = AbbonamentoSerializer

    @action(detail=False, methods=['get'], url_path='alert_attivi')
    def alert_attivi(self,request):
        alert = self.queryset.filter(user=request.user)
        serializer = self.get_serializer(alert, many=True)
        return Response(serializer.data)

class MonitoraggioViewSet(viewsets.ModelViewSet):
    queryset = Monitoraggio.objects.all()
    serializer_class = MonitoraggioSerializer

class NotificaViewSet(viewsets.ModelViewSet):
    queryset = Notifica.objects.all()
    serializer_class = NotificaSerializer

class RivenditaViewSet(viewsets.ModelViewSet):
    queryset = Rivendita.objects.all()
    serializer_class = RivenditaSerializer

class AcquistoViewSet(viewsets.ModelViewSet):
    queryset = Acquisto.objects.all()
    serializer_class = AcquistoSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return Acquisto.objects.filter(acquirente=self.request.user)

    def perform_create(self, serializer):
        rivendita = serializer.validated_data['rivendita']
        
        if not rivendita.disponibile:
            raise serializers.ValidationError('Questo biglietto non è più disponibile.')

        rivendita.disponibile = False
        rivendita.save()

        serializer.save(acquirente=self.request.user, stato='completato')
