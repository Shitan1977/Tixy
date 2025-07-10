from rest_framework import viewsets, permissions, status
from rest_framework.response import Response
from rest_framework.decorators import action, api_view, permission_classes
from django.contrib.auth import get_user_model
from .serializers import UserProfileSerializer

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
