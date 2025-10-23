# api/admin.py
from django.contrib import admin
from django.utils.html import format_html
from django.utils.timezone import localtime
from django import forms
from django.shortcuts import redirect
from django.urls import reverse_lazy
from django.utils.translation import gettext_lazy as _
from django.http import HttpRequest
from django.contrib.auth.admin import UserAdmin

from .models import (
    UserProfile, Artista, Luoghi, Categoria, Evento, Performance,
    Piattaforma, EventoPiattaforma, PerformancePiattaforma, InventorySnapshot,
    Sconti, AlertPlan, Abbonamento, Monitoraggio, Notifica, AlertTrigger, EventFollow,
    Biglietto, Listing, ListingTicket, OrderTicket, Payment,
    Rivendita, Acquisto, Recensione
)

# Custom Admin Pannel
from unfold.forms import AdminPasswordChangeForm, UserChangeForm, UserCreationForm
from unfold.admin import ModelAdmin  
from unfold.paginator import InfinitePaginator
from unfold.decorators import action


admin.site.index_title = "Tixy"

class ClickableRowAdminMixin:
    class Media:
        js = ("unfold/js/admin-row-click.js",)
        css = {
            "all": ("unfold/css/admin-row-click.css",)
        }

ModelAdmin = type("ModelAdmin", (ClickableRowAdminMixin, ModelAdmin), {})

# ============== Helper ==============

def shorten(text, n=80):
    if not text:
        return ""
    s = str(text)
    return (s[:n] + "…") if len(s) > n else s


# ============== Inlines ==============

class PerformanceInline(admin.TabularInline):
    model = Performance
    extra = 0
    fields = ("luogo", "starts_at_utc", "status", "disponibilita_agg", "prezzo_min", "prezzo_max", "valuta")
    show_change_link = True


class PerformancePiattaformaInline(admin.TabularInline):
    model = PerformancePiattaforma
    extra = 0
    fields = ("piattaforma", "external_perf_id", "url", "ultima_scansione")
    readonly_fields = ("ultima_scansione",)
    show_change_link = True


class InventorySnapshotInline(admin.TabularInline):
    model = InventorySnapshot
    extra = 0
    fields = ("piattaforma", "taken_at", "availability_status", "min_price", "max_price", "currency")
    readonly_fields = ("piattaforma", "taken_at", "availability_status", "min_price", "max_price", "currency")


# ============== User ==============

# Add: admin creation form exposing extra fields + password1/password2
class AdminUserCreationForm(forms.ModelForm):
    password1 = forms.CharField(label="Password", widget=forms.PasswordInput, required=False)
    password2 = forms.CharField(label="Password confirmation", widget=forms.PasswordInput, required=False)

    class Meta:
        model = UserProfile
        fields = (
            "email", "first_name", "last_name",
            "phone_number", "date_of_birth", "gender",
            "country", "city", "address", "zip_code", "document_id",
            "notify_email", "notify_whatsapp", "notify_push",
            "accepted_terms", "accepted_privacy",
            "is_active", "is_verified", "is_staff",
        )

    def clean(self):
        cleaned = super().clean()
        p1 = cleaned.get("password1")
        p2 = cleaned.get("password2")
        if p1 or p2:
            if p1 != p2:
                raise forms.ValidationError("Passwords do not match.")
        return cleaned

    def save(self, commit=True):
        user = super().save(commit=False)
        pwd = self.cleaned_data.get("password1")
        if pwd:
            user.set_password(pwd)
        else:
            user.set_unusable_password()
        if commit:
            user.save()
            # save m2m if any
            self.save_m2m()
        return user


@admin.register(UserProfile)
class UserProfileAdmin(ModelAdmin):
# Custom Admin Pannel
    form = UserChangeForm
    add_form = AdminUserCreationForm
    change_password_form = AdminPasswordChangeForm
    paginator = InfinitePaginator
    show_full_result_count = True


    list_display = ("id", "email", "first_name", "last_name", "is_active", "is_staff", "is_verified", "created_at")
    list_filter = ("is_active", "is_staff", "is_verified", "accepted_terms", "accepted_privacy")
    search_fields = ("email", "first_name", "last_name", "document_id")

# Custom Admin Pannel - fix ordering error and configure fieldsets
    ordering = ("email",)  # or ("-created_at",) for newest first
    
    fieldsets = (
        (None, {"fields": ("email", "password")}),
        ("Personal Info", {"fields": ("first_name", "last_name", "phone_number", "date_of_birth", "gender")}),
        ("Address", {"fields": ("country", "city", "address", "zip_code", "document_id")}),
        ("Permissions", {"fields": ("is_active", "is_staff", "is_verified", "groups", "user_permissions")}),
        ("Preferences", {"fields": ("notify_email", "notify_whatsapp", "notify_push")}),
        ("Legal", {"fields": ("accepted_terms", "accepted_privacy", "gdpr_consent_at")}),
        ("Important Dates", {"fields": ("last_login", "created_at", "updated_at", "deleted_at")}),
    )
    
    add_fieldsets = (
        (None, {
            "classes": ("wide",),
            "fields": (
                "email", "first_name", "last_name",
                "phone_number", "date_of_birth", "gender",
                "country", "city", "address", "zip_code", "document_id",
                "notify_email", "notify_whatsapp", "notify_push",
                "accepted_terms", "accepted_privacy",
                "is_active", "is_verified", "is_staff",
                #"password1", "password2",
            ),
        }),
    )
    
    # Optional: If you want to make some fields read-only
    readonly_fields = ("created_at", "updated_at", "last_login")

    def save_model(self, request, obj, form, change):
        """
        Ensure password is set and new users are active immediately when created via admin.
        Supports forms that expose 'password' or the 'password1'/'password2' pair.
        """
        if not change:
            # creation flow: set/confirm password and activate without OTP
            pwd = None
            if hasattr(form, "cleaned_data"):
                data = form.cleaned_data
                pwd = data.get("password") or data.get("password1")
            # if no password provided, keep unusable password
            if pwd:
                obj.set_password(pwd)
            else:
                obj.set_unusable_password()
            # activate immediately and mark verified if you want
            obj.is_active = True
            # optional: mark as verified to skip OTP
            obj.is_verified = True
        # Save as usual
        super().save_model(request, obj, form, change)

        actions_row = ["changelist_row_action"]

    @action(
        description=_("Changelist row action"),
        permissions=["changelist_row_action"],
        url_path="changelist-row-action",
        attrs={"target": "_blank"}
    )
    def changelist_row_action(self, request: HttpRequest, object_id: int):
        return redirect(
          reverse_lazy("admin:users_user_changelist")
        )

    def has_changelist_row_action_permission(self, request: HttpRequest):
        # Write your own bussiness logic. Code below will always display an action.
        return True
    
    class Media:
        js = ("static/unfold/js/admin-row-click.js",)

# ============== Catalogo ==============

@admin.register(Artista)
class ArtistaAdmin(ModelAdmin):
    # Custom Admin Pannel
    paginator = InfinitePaginator
    show_full_result_count = True

    list_display = ("id", "nome", "tipo", "nome_normalizzato", "creato_il", "aggiornato_il")
    search_fields = ("nome", "nome_normalizzato")
    list_filter = ("tipo",)


@admin.register(Luoghi)
class LuoghiAdmin(ModelAdmin):
    # Custom Admin Pannel
    paginator = InfinitePaginator
    show_full_result_count = True

    list_display = ("id", "nome", "citta", "stato_iso", "nome_normalizzato", "creato_il")
    search_fields = ("nome", "citta", "nome_normalizzato")
    list_filter = ("stato_iso",)


@admin.register(Categoria)
class CategoriaAdmin(ModelAdmin):
    # Custom Admin Pannel
    paginator = InfinitePaginator
    show_full_result_count = True

    list_display = ("id", "slug", "nome")
    search_fields = ("slug", "nome")


@admin.register(Evento)
class EventoAdmin(ModelAdmin):
    # Custom Admin Pannel
    paginator = InfinitePaginator
    show_full_result_count = True

    # NB: i campi rimossi dall'evento (luogo, date, prezzi, valuta, disponibilita) ora stanno in Performance
    inlines = [PerformanceInline]
    list_display = (
        "id", "slug", "nome_evento", "stato", "categoria", "artista_principale",
        "num_performances", "first_performance", "last_update",
        "has_image",
    )
    list_filter = ("stato", "categoria")
    search_fields = ("slug", "nome_evento", "nome_evento_normalizzato", "artista_principale__nome")
    readonly_fields = ("creato_il", "aggiornato_il")

    def num_performances(self, obj):
        return obj.performances.count()

    def first_performance(self, obj):
        p = obj.performances.order_by("starts_at_utc").first()
        return localtime(p.starts_at_utc).strftime("%d/%m/%Y %H:%M") if p else "-"

    def last_update(self, obj):
        return localtime(obj.aggiornato_il).strftime("%d/%m/%Y %H:%M")

    def has_image(self, obj):
        return format_html("✅") if obj.immagine_url else "—"
    has_image.short_description = "img"


@admin.register(Performance)
class PerformanceAdmin(ModelAdmin):
    # Custom Admin Pannel
    paginator = InfinitePaginator
    show_full_result_count = True

    inlines = [PerformancePiattaformaInline, InventorySnapshotInline]
    list_display = (
        "id", "evento", "luogo", "starts_at_utc", "status",
        "disponibilita_agg", "prezzo_min", "prezzo_max", "valuta"
    )
    list_filter = ("status", "disponibilita_agg", "valuta", "luogo__citta")
    search_fields = ("evento__nome_evento", "luogo__nome", "luogo__citta")
    date_hierarchy = "starts_at_utc"


# ============== Piattaforme & Scrape ==============

@admin.register(Piattaforma)
class PiattaformaAdmin(ModelAdmin):
    # Custom Admin Pannel
    paginator = InfinitePaginator
    show_full_result_count = True

    list_display = ("id", "nome", "dominio", "attivo")
    list_filter = ("attivo",)
    search_fields = ("nome", "dominio")


@admin.register(EventoPiattaforma)
class EventoPiattaformaAdmin(ModelAdmin):
    # Custom Admin Pannel
    paginator = InfinitePaginator
    show_full_result_count = True

    list_display = ("id", "evento", "piattaforma", "id_evento_piattaforma", "ultima_scansione")
    search_fields = ("evento__nome_evento", "piattaforma__nome", "id_evento_piattaforma")
    list_filter = ("piattaforma",)


@admin.register(PerformancePiattaforma)
class PerformancePiattaformaAdmin(ModelAdmin):
    # Custom Admin Pannel
    paginator = InfinitePaginator
    show_full_result_count = True

    list_display = ("id", "performance", "piattaforma", "external_perf_id", "ultima_scansione")
    search_fields = ("performance__evento__nome_evento", "external_perf_id", "piattaforma__nome")
    list_filter = ("piattaforma",)


@admin.register(InventorySnapshot)
class InventorySnapshotAdmin(ModelAdmin):
    # Custom Admin Pannel
    paginator = InfinitePaginator
    show_full_result_count = True

    list_display = ("id", "performance", "piattaforma", "taken_at", "availability_status", "min_price", "max_price", "currency")
    list_filter = ("availability_status", "currency", "piattaforma")
    search_fields = ("performance__evento__nome_evento", "piattaforma__nome")
    date_hierarchy = "taken_at"


# ============== Abbonamenti / Alert ==============

@admin.register(Sconti)
class ScontiAdmin(ModelAdmin):
    # Custom Admin Pannel
    paginator = InfinitePaginator
    show_full_result_count = True 

    list_display = ("id", "durata_mesi", "percentuale", "descrizione")


@admin.register(AlertPlan)
class AlertPlanAdmin(ModelAdmin):
    # Custom Admin Pannel
    paginator = InfinitePaginator
    show_full_result_count = True

    list_display = ("id", "name", "duration_days", "price", "currency", "max_events", "max_push_per_day")


@admin.register(Abbonamento)
class AbbonamentoAdmin(ModelAdmin):
    # Custom Admin Pannel
    paginator = InfinitePaginator
    show_full_result_count = True

    list_display = ("id", "utente", "plan", "sconto", "prezzo", "data_inizio", "data_fine", "attivo")
    list_filter = ("attivo", "plan")
    search_fields = ("utente__email",)


@admin.register(Monitoraggio)
class MonitoraggioAdmin(ModelAdmin):
    # Custom Admin Pannel
    paginator = InfinitePaginator
    show_full_result_count = True

    # NB: rimosso frequenza_secondi; ora c'e' filters_json + target evento/performance
    list_display = ("id", "abbonamento", "target", "creato_il", "aggiornato_il")
    search_fields = ("abbonamento__utente__email", "evento__nome_evento", "performance__evento__nome_evento")
    readonly_fields = ("creato_il", "aggiornato_il")

    def target(self, obj):
        if obj.performance_id:
            p = obj.performance
            return f"PERF #{p.id} - {p.evento.nome_evento} @ {p.luogo.nome}"
        if obj.evento_id:
            return f"EVENT #{obj.evento_id} - {obj.evento.nome_evento}"
        return "-"


@admin.register(Notifica)
class NotificaAdmin(ModelAdmin):
    # Custom Admin Pannel
    paginator = InfinitePaginator
    show_full_result_count = True 

    # NB: rinominati: tipo->channel, data_invio->sent_at, esito->status, testo->message
    list_display = ("id", "monitoraggio", "channel", "status", "sent_at", "message_short")
    list_filter = ("channel", "status")
    search_fields = ("monitoraggio__abbonamento__utente__email",)
    readonly_fields = ("sent_at",)

    def message_short(self, obj):
        return shorten(obj.message, 80)
    message_short.short_description = "message"


@admin.register(AlertTrigger)
class AlertTriggerAdmin(ModelAdmin):
    # Custom Admin Pannel
    paginator = InfinitePaginator
    show_full_result_count = True

    list_display = ("id", "monitoraggio", "reason", "triggered_at", "snapshot")
    list_filter = ("reason",)
    date_hierarchy = "triggered_at"


@admin.register(EventFollow)
class EventFollowAdmin(ModelAdmin):
    # Custom Admin Pannel
    paginator = InfinitePaginator
    show_full_result_count = True

    list_display = ("id", "user", "event", "created_at")
    search_fields = ("user__email", "event__nome_evento")


# ============== Marketplace ==============

@admin.register(Biglietto)
class BigliettoAdmin(ModelAdmin):
    # Custom Admin Pannel
    paginator = InfinitePaginator
    show_full_result_count = True

    list_display = ("id", "nome_file", "nome_intestatario", "sigillo_fiscale", "is_valid", "creato_il")
    list_filter = ("is_valid",)
    search_fields = ("nome_file", "nome_intestatario", "sigillo_fiscale", "hash_file")


class ListingTicketInline(admin.TabularInline):
    model = ListingTicket
    extra = 0


@admin.register(Listing)
class ListingAdmin(ModelAdmin):
    # Custom Admin Pannel
    paginator = InfinitePaginator
    show_full_result_count = True

    inlines = [ListingTicketInline]
    list_display = ("id", "seller", "performance", "qty", "price_each", "currency", "delivery_method", "status", "expires_at")
    list_filter = ("status", "currency", "delivery_method")
    search_fields = ("seller__email", "performance__evento__nome_evento", "performance__luogo__nome")


@admin.register(OrderTicket)
class OrderTicketAdmin(ModelAdmin):
    # Custom Admin Pannel
    paginator = InfinitePaginator
    show_full_result_count = True

    list_display = ("id", "buyer_email", "event_name", "qty", "formatted_total", "status", "created_at_local")
    list_filter = ("status", "listing__performance__evento__categoria", "created_at")
    search_fields = ("buyer__email", "listing__performance__evento__nome_evento", "id")
    date_hierarchy = "created_at"
    readonly_fields = ("created_at", "paid_at", "delivered_at", "buyer", "listing", "qty", "unit_price", "total_price", "currency")

    def buyer_email(self, obj):
        return obj.buyer.email
    buyer_email.short_description = "Acquirente"

    def event_name(self, obj):
        return shorten(obj.listing.performance.evento.nome_evento, 40)
    event_name.short_description = "Evento"

    def formatted_total(self, obj):
        return f"€ {obj.total_price:.2f}"
    formatted_total.short_description = "Totale Ordine"
    formatted_total.admin_order_field = "total_price"

    def created_at_local(self, obj):
        return localtime(obj.created_at).strftime("%d/%m/%Y %H:%M")
    created_at_local.short_description = "Data Ordine"
    created_at_local.admin_order_field = "created_at"

    def has_add_permission(self, request):
        return False # Disabilita la creazione di ordini dall'admin

    def has_change_permission(self, request, obj=None):
        return False # Rende gli ordini read-only


@admin.register(Payment)
class PaymentAdmin(ModelAdmin):
    # Custom Admin Pannel
    paginator = InfinitePaginator
    show_full_result_count = True

    list_display = ("id", "order", "provider", "amount", "currency", "status", "created_at")
    list_filter = ("provider", "status")
    search_fields = ("order__buyer__email",)



@admin.register(Rivendita)
class RivenditaAdmin(ModelAdmin):
    # Custom Admin Pannel
    paginator = InfinitePaginator
    show_full_result_count = True

    list_display = ("id", "evento", "venditore", "biglietto", "prezzo", "disponibile", "creato_il")
    list_filter = ("disponibile",)
    search_fields = ("evento__nome_evento", "venditore__email")


@admin.register(Acquisto)
class AcquistoAdmin(ModelAdmin):
    # Custom Admin Pannel
    paginator = InfinitePaginator
    show_full_result_count = True

    list_display = ("id", "rivendita", "acquirente", "stato", "data_acquisto")
    list_filter = ("stato",)
    search_fields = ("acquirente__email",)


# ============== Recensioni ==============

@admin.register(Recensione)
class RecensioneAdmin(ModelAdmin):
    # Custom Admin Pannel
    paginator = InfinitePaginator
    show_full_result_count = True

    list_display = ("id", "venditore", "acquirente", "rating", "order", "creato_il", "testo_short")
    list_filter = ("rating",)
    search_fields = ("venditore__email", "acquirente__email")

    def testo_short(self, obj):
        return shorten(obj.testo, 80)
    testo_short.short_description = "testo"
