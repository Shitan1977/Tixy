# api/urls.py
from django.urls import path, include
from rest_framework.response import Response
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView
from rest_framework.routers import DefaultRouter

from .views import (
    # user
    UserProfileViewSet, UserRegistrationView, ConfirmOTPView, UserProfileAPIView, PublicUserDetailView,
    # catalogo
    EventoViewSet, BigliettoUploadView,
    ArtistaViewSet, LuoghiViewSet, CategoriaViewSet, PiattaformaViewSet, EventoPiattaformaViewSet,
    # search
    PerformanceSearchViewSet, autocomplete,
    # abbonamenti / alert
    ScontiViewSet, AbbonamentoViewSet, MonitoraggioViewSet, NotificaViewSet,
    # marketplace legacy
    RivenditaViewSet, AcquistoViewSet,
    # performances (readonly con azioni /{id}/listings e /{id}/other_dates)
    PerformanceViewSet,
    # listings & orders
    ListingViewSet, OrderTicketViewSet,
    # checkout / otp
    CheckoutSummaryView, CheckoutStartView, ResendOTPView,
    # recensioni
    RecensioneViewSet, MyPurchasesView, TicketUploadViewSet, MyResalesView, SupportTicketViewSet,
    SupportAttachmentUploadView,
)

# Endpoint di test (protetto)
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def hello_world(request):
    return Response({"message": f"Hello, {request.user.email}!"})

# ---------- Router ----------
router = DefaultRouter()
router.register(r'users', UserProfileViewSet, basename='user')
router.register(r'eventi', EventoViewSet, basename='evento')

# catalogo aggiuntivi
router.register(r'artisti', ArtistaViewSet, basename='artisti')
router.register(r'luoghi', LuoghiViewSet, basename='luoghi')
router.register(r'categorie', CategoriaViewSet, basename='categorie')
router.register(r'piattaforme', PiattaformaViewSet, basename='piattaforme')
router.register(r'evento-piattaforma', EventoPiattaformaViewSet, basename='evento-piattaforma')

#assistenza
router.register(r'support/tickets', SupportTicketViewSet, basename='support-ticket')



# search
router.register(r'search/performances', PerformanceSearchViewSet, basename='search-performances')

# performances (readonly con actions)
router.register(r'performances', PerformanceViewSet, basename='performances')

# abbonamenti/alert
router.register(r'sconti', ScontiViewSet, basename='sconti')
router.register(r'abbonamenti', AbbonamentoViewSet, basename='abbonamenti')
router.register(r'monitoraggi', MonitoraggioViewSet, basename='monitoraggi')
router.register(r'notifiche', NotificaViewSet, basename='notifiche')

# marketplace legacy
router.register(r'rivendite', RivenditaViewSet, basename='rivendite')
router.register(r'acquisti', AcquistoViewSet, basename='acquisti')

# listings & orders
router.register(r'listings', ListingViewSet, basename='listings')
router.register(r'orders', OrderTicketViewSet, basename='orders')

# recensioni
router.register(r'reviews', RecensioneViewSet, basename='reviews')

#redis
router.register(r"ticket-uploads", TicketUploadViewSet, basename="ticket-upload")

# ---------- URL patterns ----------
urlpatterns = [
    # JWT
    path('auth/token/', TokenObtainPairView.as_view(), name='token_obtain_pair'),
    path('auth/token/refresh/', TokenRefreshView.as_view(), name='token_refresh'),

    # test
    path('hello/', hello_world, name='hello_world'),

    # router
    path('', include(router.urls)),

    # user direct
    path('profile/', UserProfileAPIView.as_view(), name='user-profile'),
    path('register/', UserRegistrationView.as_view(), name='user-register'),
    path('auth/confirm-otp/', ConfirmOTPView.as_view(), name='confirm-otp'),
    path('auth/resend-otp/', ResendOTPView.as_view(), name='auth-resend-otp'),
    path('public/users/<int:pk>/', PublicUserDetailView.as_view(), name='public-user-detail'),

    # checkout
    path('checkout/start/', CheckoutStartView.as_view(), name='checkout-start'),
    path('checkout/summary/<int:pk>/', CheckoutSummaryView.as_view(), name='checkout-summary'),
    path("my/purchases/", MyPurchasesView.as_view(), name="my-purchases"),

    # search helpers
    path('autocomplete/', autocomplete, name='autocomplete'),

    #redis
    path("my/resales/", MyResalesView.as_view(), name="my-resales"),

    # assistenza
    path('support/attachments/', SupportAttachmentUploadView.as_view(), name='support-attachment-upload'),

]

# ---------- Swagger ----------
try:
    from drf_yasg.views import get_schema_view
    from drf_yasg import openapi
    from django.views.generic import RedirectView

    schema_view = get_schema_view(
        openapi.Info(
            title="Tixy API",
            default_version='v1',
            description="Ticket alerts & marketplace",
        ),
        public=True,
        permission_classes=(AllowAny,),
    )

    urlpatterns += [
        path('docs/', RedirectView.as_view(url='/api/docs/swagger/'), name='docs'),
        path('docs/swagger/', schema_view.with_ui('swagger', cache_timeout=0), name='schema-swagger-ui'),
        path('docs/redoc/', schema_view.with_ui('redoc', cache_timeout=0), name='schema-redoc'),
        path('docs/schema.json', schema_view.without_ui(cache_timeout=0), name='schema-json'),
    ]
except Exception:
    pass
