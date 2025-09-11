from django.urls import path, include
from rest_framework.response import Response
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework_simplejwt.views import (TokenObtainPairView,TokenRefreshView)
from rest_framework.routers import DefaultRouter
from .views import UserProfileViewSet, UserRegistrationView, EventoViewSet, BigliettoUploadView, ConfirmOTPView, UserProfileAPIView, RecensioneViewSet, ArtistaViewSet, LuoghiViewSet, CategoriaViewSet, PiattaformaViewSet, EventoPiattaformaViewSet, ScontiViewSet, AbbonamentoViewSet, MonitoraggioViewSet, NotificaViewSet, RivenditaViewSet, AcquistoViewSet

# Rotta di test
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def hello_world(request):
    return Response({"message": f"Hello, {request.user.email}!"})


router = DefaultRouter()
router.register(r'users', UserProfileViewSet, basename='user')
router.register(r'eventi', EventoViewSet, basename='evento')
router.register(r'biglietti',BigliettoUploadView, basename='biglietti')
router.register(r'recensioni', RecensioneViewSet, basename='recensione')
router.register(r'artisti', ArtistaViewSet, basename='artista')
router.register(r'luoghi', LuoghiViewSet, basename='luogo')
router.register(r'categorie', CategoriaViewSet, basename='categoria')
router.register(r'piattaforme', PiattaformaViewSet, basename='piattaforma')
router.register(r'eventi-piattaforme', EventoPiattaformaViewSet, basename='evento-piattaforma')
router.register(r'sconti', ScontiViewSet, basename='sconto')
router.register(r'abbonamenti', AbbonamentoViewSet, basename='abbonamento')
router.register(r'monitoraggi', MonitoraggioViewSet, basename='monitoraggio')
router.register(r'notifiche', NotificaViewSet, basename='notifica')
router.register(r'rivendite', RivenditaViewSet, basename='rivendita')
router.register(r'acquisti', AcquistoViewSet, basename='acquisto')

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
