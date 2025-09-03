from django.urls import path, include
from rest_framework.response import Response
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework_simplejwt.views import (TokenObtainPairView,TokenRefreshView)
from rest_framework.routers import DefaultRouter
from .views import UserProfileViewSet, UserRegistrationView, EventoViewSet, BigliettoUploadView, ConfirmOTPView, UserProfileAPIView


# Rotta di test
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def hello_world(request):
    return Response({"message": f"Hello, {request.user.email}!"})

# Router per UserProfile
router = DefaultRouter()

router.register(r'users', UserProfileViewSet, basename='user')

router.register(r'eventi', EventoViewSet, basename='evento')

router.register(r'biglietti',BigliettoUploadView, basename='biglietti')

urlpatterns = [

    # JWT Auth
    path('token/', TokenObtainPairView.as_view(), name='token_obtain_pair'),
    path('token/refresh/', TokenRefreshView.as_view(), name='token_refresh'),

    # Hello World di test
    path('hello/', hello_world, name='hello_world'),

    # API UserProfile
    path('', include(router.urls)),
    path('profile/', UserProfileAPIView.as_view(), name='user-profile'),

    #registrazione utenti api pubblica
    path('register/', UserRegistrationView.as_view(), name='user-register'),

    #otp
    path('auth/confirm-otp/', ConfirmOTPView.as_view(), name='confirm-otp'),
]
