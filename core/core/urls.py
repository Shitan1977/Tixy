from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from rest_framework import permissions
from drf_yasg.views import get_schema_view
from drf_yasg import openapi

#aggiungiamo lo schema
schema_view = get_schema_view(
    openapi.Info(
        title="API di Tixy",
        default_version='v1',
        description="Documentazione delle API del progetto Tixy",
    ),
    public=True,
    permission_classes=(permissions.AllowAny,),
)

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/', include('api.urls')),
    path('swagger/', schema_view.with_ui('swagger', cache_timeout=0), name='schema-swagger-ui'),
    path('redoc/', schema_view.with_ui('redoc', cache_timeout=0), name='schema-redoc'),
]

#Per aprire e visualizzare i file caricati con settings.DEBUG = True
# Media in debug (se ti servono)
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

# Statici (admin + Unfold + DRF + drf-yasg)
urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
