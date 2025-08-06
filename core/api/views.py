from drf_yasg.utils import swagger_auto_schema
from rest_framework import viewsets, permissions, status, generics, filters
from rest_framework.response import Response
from rest_framework.decorators import action
from rest_framework.parsers import MultiPartParser, FormParser
from django.contrib.auth import get_user_model
from django.utils.timezone import now
from django_filters.rest_framework import DjangoFilterBackend
from .models import Evento, Biglietto
from .serializers import UserProfileSerializer, UserRegistrationSerializer, EventoSerializer, BigliettoUploadSerializer,OTPVerificationSerializer
from rest_framework.views import APIView

User = get_user_model()

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

#registrazione pubblica
class UserRegistrationView(generics.CreateAPIView):
    queryset = User.objects.all()
    serializer_class = UserRegistrationSerializer
    permission_classes = [permissions.AllowAny]

class EventoViewSet(viewsets.ModelViewSet):
    queryset = Evento.objects.all()
    serializer_class = EventoSerializer
    filter_backends = [filters.SearchFilter, filters.OrderingFilter, DjangoFilterBackend]
    search_fields = ['nome_evento', 'artista', 'citta', 'luogo']
    ordering_fields = ['data_ora']
    filterset_fields = ['citta', 'categoria', 'artista', 'stato_disponibilita']

    def get_permissions(self):
        if self.request.method in ['GET', 'HEAD', 'OPTIONS']:
            return [permissions.AllowAny]
        return [IsAdminOrIsSelf]

    def get_queryset(self):
        qs = super().get_queryset()
        qs = qs.filter(attivo=True, data_ora__gte=now())
        da_data = self.request.query_params.get('da_data')
        a_data = self.request.query_params.get('a_data')
        if da_data:
            qs = qs.filter(data_ora__gte=da_data)
        if a_data:
            qs = qs.filter(data_ora__lte=a_data)
        return qs
#otp via email
class ConfirmOTPView(APIView):

    @swagger_auto_schema(
        request_body=OTPVerificationSerializer,
        operation_summary="Conferma registrazione OTP",
        operation_description="Inserisci email e codice OTP ricevuto via email per completare la registrazione."
    )
    def post(self, request):
        serializer = OTPVerificationSerializer(data=request.data)
        if serializer.is_valid():
            return Response(serializer.validated_data, status=200)
        return Response(serializer.errors, status=400)

# Parte dell'upload dei File
class BigliettoUploadView(viewsets.ModelViewSet):
    queryset = Biglietto.objects.all()
    serializer_class = BigliettoUploadSerializer
    #permission_classes = [IsAdminOrIsSelf]
    parser_classes = [MultiPartParser, FormParser]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['titolo','data_caricamento']
    ordering_fields = ['data_caricamento']
    filterset_fields = ['tipo_biglietto','titolo']
