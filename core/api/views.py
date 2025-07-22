from rest_framework import viewsets, permissions, status, generics, filters
from rest_framework.response import Response
from rest_framework.decorators import action, api_view, permission_classes
from django.contrib.auth import get_user_model
from django.utils.timezone import now
from django_filters.rest_framework import DjangoFilterBackend
from .models import Evento
from .serializers import UserProfileSerializer, UserRegistrationSerializer, EventoSerializer

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

    def get_permissions(self):
        if self.action in ['list', 'destroy', 'create']:
            permission_classes = [permissions.IsAdminUser]
        elif self.action in ['retrieve', 'update', 'partial_update']:
            permission_classes = [IsAdminOrIsSelf]
        else:
            permission_classes = [permissions.IsAuthenticated]
        return [permission() for permission in permission_classes]

    @action(detail=False, methods=['get'], permission_classes=[permissions.IsAuthenticated])
    def me(self, request):
        """
        Restituisce i dati del proprio profilo.
        """
        serializer = self.get_serializer(request.user)
        return Response(serializer.data)

    @action(detail=False, methods=['delete'], permission_classes=[permissions.IsAuthenticated])
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
    search_fields = ['nome_evento', 'artista', 'città', 'luogo']
    ordering_fields = ['data_ora']
    filterset_fields = ['città', 'categoria', 'artista', 'stato_disponibilità']

    def get_permissions(self):
        if self.request.method in ['GET', 'HEAD', 'OPTIONS']:
            return [permissions.AllowAny()]
        return [permissions.IsAuthenticated()]

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