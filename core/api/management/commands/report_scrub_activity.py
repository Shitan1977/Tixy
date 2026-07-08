from datetime import timedelta

from django.conf import settings
from django.core.mail import send_mail
from django.core.management.base import BaseCommand
from django.utils import timezone


class Command(BaseCommand):
    help = "Invia via email un report dei dati creati/aggiornati nelle ultime N ore (controllo scrub)."

    def add_arguments(self, parser):
        parser.add_argument("--hours", type=int, default=24)
        parser.add_argument("--label", type=str, default="report")
        parser.add_argument("--to", type=str, default="info@freestyleweb.it")
        parser.add_argument("--dry-run", action="store_true",
                            help="Stampa il report senza inviare email.")

    def handle(self, *args, **options):
        hours = options["hours"]
        label = options["label"]
        to_email = options["to"]
        dry_run = options["dry_run"]

        from api.models import (
            Evento, Performance,
            PerformancePiattaforma, EventoPiattaforma, Notifica,
        )

        now = timezone.now()
        since = now - timedelta(hours=hours)

        eventi_nuovi = Evento.objects.filter(creato_il__gte=since).count()
        perf_nuove = Performance.objects.filter(creato_il__gte=since).count()

        pp_nuovi_rows = (
            PerformancePiattaforma.objects
            .filter(creato_il__gte=since)
            .values_list("piattaforma__nome", flat=True)
        )
        pp_per_piattaforma = {}
        for nome in pp_nuovi_rows:
            key = (nome or "?").lower()
            pp_per_piattaforma[key] = pp_per_piattaforma.get(key, 0) + 1

        ep_nuovi = EventoPiattaforma.objects.filter(creato_il__gte=since).count()

        # copertura mapping delle performance monitorate (PRO attivi)
        from api.models import Monitoraggio
        mon_perf = set(Monitoraggio.objects.filter(
            abbonamento__attivo=True, abbonamento__prezzo__gt=0,
            performance__starts_at_utc__gte=now).values_list("performance_id", flat=True))
        pp_ids = set(PerformancePiattaforma.objects.filter(
            performance_id__in=mon_perf).values_list("performance_id", flat=True))
        mon_zero = len(mon_perf - pp_ids)

        # copertura mapping delle performance monitorate (PRO attivi)
        from api.models import Monitoraggio
        mon_perf = set(Monitoraggio.objects.filter(
            abbonamento__attivo=True, abbonamento__prezzo__gt=0,
            performance__starts_at_utc__gte=now).values_list("performance_id", flat=True))
        pp_ids = set(PerformancePiattaforma.objects.filter(
            performance_id__in=mon_perf).values_list("performance_id", flat=True))
        mon_zero = len(mon_perf - pp_ids)

        notifiche_sent = Notifica.objects.filter(status="SENT", sent_at__gte=since).count()
        notifiche_failed = Notifica.objects.filter(status="FAILED", creato_il__gte=since).count() \
            if hasattr(Notifica, "creato_il") else Notifica.objects.filter(status="FAILED").count()

        import pytz
        rome = pytz.timezone("Europe/Rome")
        righe = [
            f"Report Tixy [{label}] — ultime {hours}h",
            f"Generato: {now.astimezone(rome).strftime('%d/%m/%Y %H:%M')} (ora italiana)",
            "",
            f"Eventi nuovi:                {eventi_nuovi}",
            f"Performance nuove:           {perf_nuove}",
            f"Mapping evento nuovi (EP):   {ep_nuovi}",
            "Mapping performance nuovi (PP) per piattaforma:",
        ]
        if pp_per_piattaforma:
            for nome, n in sorted(pp_per_piattaforma.items()):
                righe.append(f"  - {nome}: {n}")
        else:
            righe.append("  - nessuno")
        righe += [
            "",
            f"Perf monitorate senza mapping: {mon_zero} su {len(mon_perf)}",
            f"Perf monitorate senza mapping: {mon_zero} su {len(mon_perf)}",
            f"Notifiche inviate (SENT):    {notifiche_sent}",
            f"Notifiche fallite (FAILED):  {notifiche_failed}",
        ]
        if eventi_nuovi == 0 and perf_nuove == 0:
            righe += ["", "⚠️  ATTENZIONE: zero eventi e zero performance nel periodo. Verificare gli scrub!"]

        body = "\n".join(righe)
        subject = f"[Tixy] Report scrub [{label}] — {eventi_nuovi} eventi, {perf_nuove} perf nelle ultime {hours}h"

        self.stdout.write(body)
        if dry_run:
            self.stdout.write(self.style.WARNING("[DRY-RUN] email non inviata"))
            return
        send_mail(
            subject=subject, message=body,
            from_email=getattr(settings, "DEFAULT_FROM_EMAIL", None),
            recipient_list=[to_email], fail_silently=False,
        )
        self.stdout.write(self.style.SUCCESS(f"[OK] report inviato a {to_email}"))
