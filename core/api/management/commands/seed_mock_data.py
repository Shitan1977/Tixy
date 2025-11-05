# api/management/commands/seed_mock_data.py
from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model
from django.utils import timezone
from datetime import timedelta
from decimal import Decimal
from django.db import transaction
import re
import hashlib

from api.models import (
    Artista, Luoghi, Categoria, Evento, Performance,
    Piattaforma, EventoPiattaforma, PerformancePiattaforma,
    InventorySnapshot,
    Listing, ListingTicket, OrderTicket, Payment,
    Recensione,
    # opzionale se esistono collegamenti:
    # Rivendita, Acquisto,
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
    help = "Seed mock (TOP e non-TOP) + reset opzionale (--reset)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--reset",
            action="store_true",
            help="Svuota le tabelle in ordine sicuro prima del seed."
        )

    @transaction.atomic
    def handle(self, *args, **opts):
        now = timezone.now()

        # ---------- RESET (opzionale) ----------
        if opts.get("reset"):
            self.stdout.write("🧹 Reset tabelle principali…")
            # Ordine importante per FK con PROTECT/CASCADE
            # 1) pagamenti/ordini
            Payment.objects.all().delete()
            OrderTicket.objects.all().delete()
            # 2) listing & legami listing
            ListingTicket.objects.all().delete()
            Recensione.objects.all().delete()
            Listing.objects.all().delete()
            # 3) mapping, snapshot, performance/eventi
            PerformancePiattaforma.objects.all().delete()
            EventoPiattaforma.objects.all().delete()
            InventorySnapshot.objects.all().delete()
            Performance.objects.all().delete()
            Evento.objects.all().delete()
            # 4) anagrafiche
            Luoghi.objects.all().delete()
            Artista.objects.all().delete()
            Categoria.objects.all().delete()
            # Nota: lascio Piattaforma, ma puoi azzerarla se vuoi
            Piattaforma.objects.all().delete()

        # ---------- 1) PIATTAFORME ----------
        ticketone, _ = Piattaforma.objects.get_or_create(
            nome="TicketOne",
            defaults=dict(dominio="ticketone.it", attivo=True)
        )

        # ---------- 2) CATEGORIE ----------
        cat_concerti, _ = Categoria.objects.get_or_create(slug="concerti", defaults={"nome": "Concerti"})
        cat_teatro,   _ = Categoria.objects.get_or_create(slug="teatro",   defaults={"nome": "Teatro"})

        # ---------- 3) LUOGHI ----------
        olimpico, _ = Luoghi.objects.get_or_create(
            nome="Stadio Olimpico",
            nome_normalizzato=normalize("Stadio Olimpico - Roma"),
            defaults=dict(indirizzo="Viale dei Gladiatori", citta="Roma",
                          citta_normalizzata=normalize("Roma"), stato_iso="IT")
        )
        maradona, _ = Luoghi.objects.get_or_create(
            nome="Stadio Diego Armando Maradona",
            nome_normalizzato=normalize("Stadio Diego Armando Maradona - Napoli"),
            defaults=dict(indirizzo="Piazzale Tecchio", citta="Napoli",
                          citta_normalizzata=normalize("Napoli"), stato_iso="IT")
        )
        teatro_colosseo, _ = Luoghi.objects.get_or_create(
            nome="Teatro Colosseo",
            nome_normalizzato=normalize("Teatro Colosseo - Torino"),
            defaults=dict(indirizzo="Via Madama Cristina", citta="Torino",
                          citta_normalizzata=normalize("Torino"), stato_iso="IT")
        )

        # ---------- 4) ARTISTI ----------
        coldplay, _ = Artista.objects.get_or_create(
            nome="Coldplay",
            defaults=dict(tipo="artista", nome_normalizzato=normalize("Coldplay"))
        )
        ultimo, _   = Artista.objects.get_or_create(
            nome="Ultimo",
            defaults=dict(tipo="artista", nome_normalizzato=normalize("Ultimo"))
        )
        pirandello, _ = Artista.objects.get_or_create(
            nome="Pirandello – Il berretto a sonagli",
            defaults=dict(tipo="altro", nome_normalizzato=normalize("Pirandello Il berretto a sonagli"))
        )

        # ---------- 5) EVENTI ----------
        def create_event(nome_evento, artista, categoria):
            slug = slugify(nome_evento)
            nome_norm = normalize(nome_evento)
            hash_can = unique_hash(nome_norm, str(getattr(artista, "id", "")), str(getattr(categoria, "id", "")))
            ev, created = Evento.objects.get_or_create(
                slug=slug,
                defaults=dict(
                    nome_evento=nome_evento,
                    nome_evento_normalizzato=nome_norm,
                    descrizione="",
                    stato="pianificato",
                    genere=None,
                    lingua="it",
                    immagine_url="",
                    artista_principale=artista,
                    categoria=categoria,
                    hash_canonico=hash_can,
                    note_raw=None,
                )
            )
            if not created:
                # completa eventuali campi mancanti
                updated = False
                if not ev.hash_canonico:
                    ev.hash_canonico = hash_can; updated = True
                if not ev.nome_evento_normalizzato:
                    ev.nome_evento_normalizzato = nome_norm; updated = True
                if updated:
                    ev.save(update_fields=["hash_canonico", "nome_evento_normalizzato"])
            return ev

        ev_coldplay = create_event("Coldplay – Music of the Spheres Tour (Italia)", coldplay, cat_concerti)
        ev_ultimo   = create_event("Ultimo – Stadi 2025", ultimo, cat_concerti)
        ev_pira     = create_event("Il berretto a sonagli", pirandello, cat_teatro)

        # ---------- 6) PERFORMANCE ----------
        def perf(evento, luogo, in_days, hour=21, disp="disponibile",
                 status="ONSALE", pmin="50.00", pmax="150.00", valuta="EUR"):
            start = (now + timedelta(days=in_days)).replace(hour=hour, minute=0, second=0, microsecond=0)
            obj, _ = Performance.objects.get_or_create(
                evento=evento, luogo=luogo, starts_at_utc=start,
                defaults=dict(
                    ends_at_utc=None, doors_at_utc=None, status=status,
                    disponibilita_agg=disp, prezzo_min=Decimal(pmin), prezzo_max=Decimal(pmax), valuta=valuta
                )
            )
            return obj

        # diverse date per riempire "eventi del mese" e "ultimi eventi"
        perf_coldplay_rm = perf(ev_coldplay, olimpico, 7, 21, disp="disponibile", pmin="89.00", pmax="220.00")
        perf_coldplay_na = perf(ev_coldplay, maradona, 15, 21, disp="limitata", pmin="99.00", pmax="250.00")
        perf_ultimo_rm   = perf(ev_ultimo,   olimpico, 25, 21, disp="disponibile", pmin="45.00", pmax="120.00")
        perf_pira_to     = perf(ev_pira, teatro_colosseo, 12, 20, disp="disponibile", pmin="25.00", pmax="55.00")

        # ---------- 7) MAPPINGS ----------
        def ensure_event_map(evento, piattaforma, ext_id, url):
            EventoPiattaforma.objects.get_or_create(
                evento=evento, piattaforma=piattaforma,
                defaults=dict(id_evento_piattaforma=ext_id, url=url,
                              ultima_scansione=now, snapshot_raw=None, checksum_dati=None)
            )

        def ensure_perf_map(performance, piattaforma, ext_id, url):
            PerformancePiattaforma.objects.get_or_create(
                performance=performance, piattaforma=piattaforma, external_perf_id=ext_id,
                defaults=dict(url=url, ultima_scansione=now, snapshot_raw=None, checksum_dati=None)
            )

        ensure_event_map(ev_coldplay, ticketone, "T1-CLD-IT-001", "https://www.ticketone.it/event/coldplay-italia")
        ensure_event_map(ev_ultimo,   ticketone, "T1-ULT-IT-001", "https://www.ticketone.it/event/ultimo-stadi")
        ensure_event_map(ev_pira,     ticketone, "T1-PIRA-IT-001", "https://www.ticketone.it/event/pirandello-berretto")

        ensure_perf_map(perf_coldplay_rm, ticketone, "T1-CLD-RM-01", "https://www.ticketone.it/perf/CLD-RM-01")
        ensure_perf_map(perf_coldplay_na, ticketone, "T1-CLD-NA-01", "https://www.ticketone.it/perf/CLD-NA-01")
        ensure_perf_map(perf_ultimo_rm,   ticketone, "T1-ULT-RM-01", "https://www.ticketone.it/perf/ULT-RM-01")
        ensure_perf_map(perf_pira_to,     ticketone, "T1-PIRA-TO-01", "https://www.ticketone.it/perf/PIRA-TO-01")

        # ---------- 8) SELLER (TOP e NON-TOP) ----------
        sellers = []
        for i in range(1, 6):
            u, _ = User.objects.get_or_create(
                email=f"seller{i}@demo.tixy",
                defaults=dict(first_name=f"Seller{i}", last_name="Demo", is_active=True)
            )
            if not u.has_usable_password():
                u.set_password("Seller!123")
                u.save()
            sellers.append(u)

        # ---------- 9) LISTING (abbondanti: TOP e NON-TOP)
        # NB: richiede campo Listing.is_top = models.BooleanField(default=False, db_index=True)
        def mk_listing(seller, perf, price, qty, seat_cat, section, row, from_n, to_n, top=False, method="E_TICKET"):
            obj, created = Listing.objects.get_or_create(
                seller=seller, performance=perf, seat_category=seat_cat, section=section, row=row,
                seat_from=from_n, seat_to=to_n,
                defaults=dict(
                    qty=qty, price_each=Decimal(str(price)), currency="EUR",
                    delivery_method=method, status="ACTIVE",
                    notes="Mock demo",
                    is_top=top
                )
            )
            if not created and obj.is_top != top:
                obj.is_top = top
                obj.save(update_fields=["is_top"])
            return obj

        # TOP su Coldplay Napoli + Roma, NON-TOP misti
        mk_listing(sellers[0], perf_coldplay_na, 180, 2, "Distinti", "Distinti Inferiori", "12", 21, 22, top=True,  method="PDF")
        mk_listing(sellers[1], perf_coldplay_na, 175, 2, "Distinti", "Distinti Inferiori", "13", 11, 12, top=True)
        mk_listing(sellers[2], perf_coldplay_rm, 150, 2, "Curva",    "Curva Sud",         "18", 31, 32, top=True)
        mk_listing(sellers[3], perf_coldplay_rm, 120, 2, "Curva",    "Curva Nord",        "20", 41, 42, top=False)
        mk_listing(sellers[4], perf_coldplay_rm, 210, 2, "Tribuna",  "Monte Mario",       "05",  7,  8, top=True)

        # Ultimo (misti)
        mk_listing(sellers[0], perf_ultimo_rm,  90, 2, "Curva",   "Curva Sud", "22", 15, 16, top=False)
        mk_listing(sellers[1], perf_ultimo_rm, 110, 2, "Distinti","Distinti",  "11",  3,  4, top=True)

        # Teatro (IN GENERE non top, ma mettiamone uno)
        mk_listing(sellers[2], perf_pira_to, 35, 2, "Platea", "A", "03", 10, 11, top=False)
        mk_listing(sellers[3], perf_pira_to, 45, 2, "Platea", "B", "02",  5,  6, top=True)

        # ---------- 10) RECENSIONI per popolare rating venditore ----------
        buyers = []
        for i in range(1, 5):
            b, _ = User.objects.get_or_create(
                email=f"buyer{i}@demo.tixy",
                defaults=dict(first_name=f"Buyer{i}", last_name="Demo", is_active=True)
            )
            buyers.append(b)

        # 2-3 recensioni su qualche seller
        for rating in [5, 5, 4]:
            Recensione.objects.get_or_create(
                venditore=sellers[0], acquirente=buyers[(rating % len(buyers)) - 1],
                rating=rating, testo="Consegna rapida, tutto ok."
            )
        for rating in [5, 4]:
            Recensione.objects.get_or_create(
                venditore=sellers[1], acquirente=buyers[(rating % len(buyers)) - 1],
                rating=rating, testo="Esperienza positiva."
            )

        self.stdout.write(self.style.SUCCESS("✅ Seed completato: TOP e NON-TOP creati."))
