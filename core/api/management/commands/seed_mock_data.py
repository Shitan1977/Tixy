# api/management/commands/seed_mock_data.py
from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model
from django.utils import timezone
from datetime import timedelta
from decimal import Decimal
import hashlib
import re

from api.models import (
    Artista, Luoghi, Categoria, Evento, Performance,
    Piattaforma, EventoPiattaforma, PerformancePiattaforma,
    Listing, Recensione
)

User = get_user_model()

def slugify(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"\s+", "-", s)
    s = re.sub(r"-{2,}", "-", s)
    return s[:250] or "slug"

def normalize(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"\s+", " ", s)
    return s

def unique_hash(*parts) -> str:
    raw = "|".join([p or "" for p in parts])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

class Command(BaseCommand):
    help = "Seed mock per Ultimi Eventi & Top Venditore (compatibile con i tuoi models)."

    def handle(self, *args, **opts):
        now = timezone.now()

        # -----------------------------
        # 1) Piattaforma
        # -----------------------------
        ticketone, _ = Piattaforma.objects.get_or_create(
            nome="TicketOne",
            defaults=dict(dominio="ticketone.it", attivo=True)
        )

        # -----------------------------
        # 2) Categorie
        # -----------------------------
        cat_concerti, _ = Categoria.objects.get_or_create(
            slug="concerti",
            defaults=dict(nome="Concerti")
        )
        cat_teatro, _ = Categoria.objects.get_or_create(
            slug="teatro",
            defaults=dict(nome="Teatro")
        )

        # -----------------------------
        # 3) Luoghi (nome_normalizzato deve essere UNIVOCO)
        # -----------------------------
        olimpico, _ = Luoghi.objects.get_or_create(
            nome="Stadio Olimpico",
            nome_normalizzato=normalize("Stadio Olimpico - Roma"),
            defaults=dict(
                indirizzo="Viale dei Gladiatori",
                citta="Roma",
                citta_normalizzata=normalize("Roma"),
                stato_iso="IT",
            )
        )
        maradona, _ = Luoghi.objects.get_or_create(
            nome="Stadio Diego Armando Maradona",
            nome_normalizzato=normalize("Stadio Diego Armando Maradona - Napoli"),
            defaults=dict(
                indirizzo="Piazzale Tecchio",
                citta="Napoli",
                citta_normalizzata=normalize("Napoli"),
                stato_iso="IT",
            )
        )
        teatro_colosseo, _ = Luoghi.objects.get_or_create(
            nome="Teatro Colosseo",
            nome_normalizzato=normalize("Teatro Colosseo - Torino"),
            defaults=dict(
                indirizzo="Via Madama Cristina",
                citta="Torino",
                citta_normalizzata=normalize("Torino"),
                stato_iso="IT",
            )
        )

        # -----------------------------
        # 4) Artisti / Spettacolo
        # -----------------------------
        coldplay, _ = Artista.objects.get_or_create(
            nome="Coldplay",
            defaults=dict(tipo="artista", nome_normalizzato=normalize("Coldplay"))
        )
        ultimo, _ = Artista.objects.get_or_create(
            nome="Ultimo",
            defaults=dict(tipo="artista", nome_normalizzato=normalize("Ultimo"))
        )
        pirandello_show, _ = Artista.objects.get_or_create(
            nome="Pirandello – Il berretto a sonagli",
            defaults=dict(tipo="altro", nome_normalizzato=normalize("Pirandello Il berretto a sonagli"))
        )

        # -----------------------------
        # 5) Eventi (slug + hash_canonico univoci, stato = 'pianificato')
        # -----------------------------
        def create_event(nome_evento, artista, categoria, immagine_url=None):
            slug = slugify(nome_evento)
            nome_norm = normalize(nome_evento)
            hash_can = unique_hash(nome_norm, artista and str(artista.id), categoria and str(categoria.id))
            ev, created = Evento.objects.get_or_create(
                slug=slug,
                defaults=dict(
                    nome_evento=nome_evento,
                    nome_evento_normalizzato=nome_norm,
                    descrizione="",
                    stato="pianificato",     # <- tra le tue scelte
                    genere=None,
                    lingua="it",
                    immagine_url=immagine_url or "",
                    artista_principale=artista,
                    categoria=categoria,
                    hash_canonico=hash_can,
                    note_raw=None,
                )
            )
            if not created:
                # assicurati che hash/normalizzato ci siano (se esistenti più vecchi)
                update = {}
                if not ev.hash_canonico:
                    update["hash_canonico"] = hash_can
                if not ev.nome_evento_normalizzato:
                    update["nome_evento_normalizzato"] = nome_norm
                if update:
                    for k, v in update.items():
                        setattr(ev, k, v)
                    ev.save(update_fields=list(update.keys()))
            return ev

        ev_coldplay = create_event(
            "Coldplay – Music of the Spheres Tour (Italia)",
            artista=coldplay, categoria=cat_concerti
        )
        ev_ultimo = create_event(
            "Ultimo – Stadi 2025",
            artista=ultimo, categoria=cat_concerti
        )
        ev_pirandello = create_event(
            "Il berretto a sonagli",
            artista=pirandello_show, categoria=cat_teatro
        )

        # -----------------------------
        # 6) Performance (disponibilita_agg usa le tue scelte)
        # -----------------------------
        def perf(evento, luogo, in_days, hour=21, disp="disponibile", status="ONSALE",
                 pmin="50.00", pmax="150.00", valuta="EUR"):
            start = (now + timedelta(days=in_days)).replace(hour=hour, minute=0, second=0, microsecond=0)
            return Performance.objects.get_or_create(
                evento=evento, luogo=luogo, starts_at_utc=start,
                defaults=dict(
                    ends_at_utc=None,
                    doors_at_utc=None,
                    status=status,
                    disponibilita_agg=disp,    # 'disponibile' | 'limitata' | 'non_disponibile' | 'sconosciuta'
                    prezzo_min=Decimal(pmin),
                    prezzo_max=Decimal(pmax),
                    valuta=valuta
                )
            )[0]

        perf_coldplay_rm = perf(ev_coldplay, olimpico, 20, 21, disp="disponibile", pmin="89.00", pmax="220.00")
        perf_coldplay_na = perf(ev_coldplay, maradona, 27, 21, disp="limitata", pmin="99.00", pmax="250.00")
        perf_ultimo_rm   = perf(ev_ultimo,   olimpico, 14, 21, disp="disponibile", pmin="45.00", pmax="120.00")
        perf_pir_to      = perf(ev_pirandello, teatro_colosseo, 10, 20, disp="disponibile", pmin="25.00", pmax="55.00")

        # -----------------------------
        # 7) Mapping Evento/Performance <-> Piattaforma
        # (ultima_scansione è obbligatoria nei tuoi models)
        # -----------------------------
        def ensure_event_map(evento, piattaforma, ext_id, url):
            EventoPiattaforma.objects.get_or_create(
                evento=evento, piattaforma=piattaforma,
                defaults=dict(
                    id_evento_piattaforma=ext_id,
                    url=url,
                    ultima_scansione=now,  # richiesto
                    snapshot_raw=None,
                    checksum_dati=None
                )
            )

        def ensure_perf_map(performance, piattaforma, ext_id, url):
            PerformancePiattaforma.objects.get_or_create(
                performance=performance, piattaforma=piattaforma,
                external_perf_id=ext_id,
                defaults=dict(
                    url=url,
                    ultima_scansione=now,  # richiesto
                    snapshot_raw=None,
                    checksum_dati=None
                )
            )

        ensure_event_map(ev_coldplay, ticketone, "T1-CLD-IT-001", "https://www.ticketone.it/event/coldplay-italia")
        ensure_event_map(ev_ultimo,   ticketone, "T1-ULT-IT-001", "https://www.ticketone.it/event/ultimo-stadi")
        ensure_event_map(ev_pirandello, ticketone, "T1-PIRA-IT-001", "https://www.ticketone.it/event/pirandello-berretto")

        ensure_perf_map(perf_coldplay_rm, ticketone, "T1-CLD-RM-01", "https://www.ticketone.it/perf/CLD-RM-01")
        ensure_perf_map(perf_coldplay_na, ticketone, "T1-CLD-NA-01", "https://www.ticketone.it/perf/CLD-NA-01")
        ensure_perf_map(perf_ultimo_rm,   ticketone, "T1-ULT-RM-01", "https://www.ticketone.it/perf/ULT-RM-01")
        ensure_perf_map(perf_pir_to,      ticketone, "T1-PIRA-TO-01", "https://www.ticketone.it/perf/PIRA-TO-01")

        # -----------------------------
        # 8) Top venditore + Listing + Recensioni
        # (UserProfile è il tuo AUTH_USER_MODEL)
        # -----------------------------
        seller, _ = User.objects.get_or_create(
            email="top.seller@misteralert.com",
            defaults=dict(first_name="Top", last_name="Seller", is_active=True)
        )
        if not seller.has_usable_password():
            seller.set_password("Seller!123")
            seller.save()

        listing, _ = Listing.objects.get_or_create(
            seller=seller,
            performance=perf_coldplay_na,
            defaults=dict(
                seat_category="Distinti",
                section="Distinti Inferiori",
                row="12",
                seat_from=21,
                seat_to=22,
                qty=2,
                price_each=Decimal("180.00"),
                currency="EUR",
                delivery_method="PDF",   # tra le tue scelte: E_TICKET | PDF | COURIER | APP_TRANSFER
                status="ACTIVE",
                notes="Coppia adiacente, visibilità ottima"
            )
        )

        # Recensioni (richiedono 'testo' obbligatorio)
        buyers = []
        for i in range(1, 4):
            u, _ = User.objects.get_or_create(
                email=f"buyer{i}@mail.com",
                defaults=dict(first_name=f"Buyer{i}", last_name="Test", is_active=True)
            )
            buyers.append(u)

        for i, r in enumerate([5, 5, 4], start=1):
            Recensione.objects.get_or_create(
                venditore=seller,
                acquirente=buyers[i-1],
                rating=r,
                testo="Consegna rapida, biglietti perfetti."
            )

        self.stdout.write(self.style.SUCCESS("✅ Mock creati con successo."))
