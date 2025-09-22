# api/filters.py
import datetime
from django.db.models import Q
from django.utils.dateparse import parse_date
import django_filters as df
from .models import Performance, Evento

def _parse_date_any(s: str):
    s = (s or "").strip()
    if not s:
        return None
    # supporta "gg/mm/aaaa" dalla UI e "yyyy-mm-dd"
    try:
        return datetime.datetime.strptime(s, "%d/%m/%Y").date()
    except ValueError:
        return parse_date(s)  # tenta YYYY-MM-DD

class PerformanceSearchFilter(df.FilterSet):
    q = df.CharFilter(method="filter_q", help_text="testo su evento/artista/venue/citta")
    date_from = df.CharFilter(method="filter_date_from", help_text="gg/mm/aaaa o yyyy-mm-dd")
    date_to = df.CharFilter(method="filter_date_to", help_text="gg/mm/aaaa o yyyy-mm-dd")
    city = df.CharFilter(field_name="luogo__citta", lookup_expr="icontains")
    category = df.NumberFilter(field_name="evento__categoria_id")
    availability = df.CharFilter(field_name="disponibilita_agg", lookup_expr="iexact")
    platform = df.NumberFilter(method="filter_platform", help_text="piattaforma id")

    class Meta:
        model = Performance
        fields = ["q", "date_from", "date_to", "city", "category", "availability", "platform"]

    def filter_q(self, qs, name, value):
        v = (value or "").strip()
        if not v:
            return qs
        return qs.filter(
            Q(evento__nome_evento__icontains=v) |
            Q(evento__nome_evento_normalizzato__icontains=v) |
            Q(evento__artista_principale__nome__icontains=v) |
            Q(luogo__nome__icontains=v) |
            Q(luogo__citta__icontains=v)
        )

    def filter_date_from(self, qs, name, value):
        d = _parse_date_any(value)
        return qs.filter(starts_at_utc__date__gte=d) if d else qs

    def filter_date_to(self, qs, name, value):
        d = _parse_date_any(value)
        return qs.filter(starts_at_utc__date__lte=d) if d else qs

    def filter_platform(self, qs, name, value):
        # performances con un mapping su quella piattaforma
        return qs.filter(mappings__piattaforma_id=value)


class EventSearchFilter(df.FilterSet):
    q = df.CharFilter(method="filter_q")
    category = df.NumberFilter(field_name="categoria_id")

    class Meta:
        model = Evento
        fields = ["q", "category"]

    def filter_q(self, qs, name, value):
        v = (value or "").strip()
        if not v:
            return qs
        return qs.filter(
            Q(nome_evento__icontains=v) |
            Q(nome_evento_normalizzato__icontains=v) |
            Q(artista_principale__nome__icontains=v)
        )
