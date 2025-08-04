# core/api/swagger.py

from drf_yasg.views import get_schema_view
from drf_yasg import openapi
from rest_framework import permissions

schema_view = get_schema_view(
    openapi.Info(
        title="Tixy API",
        default_version='v1',
        description="API ufficiali per Tixy - gestione utenti, eventi, piattaforme e biglietti",
        contact=openapi.Contact(email="support@misteralert.it"),
        license=openapi.License(name="MIT License"),
    ),
    public=True,
    permission_classes=[permissions.AllowAny],
)
