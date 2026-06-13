from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0004_pushdevice"),
    ]

    operations = [
        migrations.AddField(
            model_name="alertplan",
            name="periodo",
            field=models.CharField(
                blank=True,
                choices=[
                    ("1m", "1 mese"),
                    ("3m", "3 mesi"),
                    ("6m", "6 mesi"),
                    ("12m", "12 mesi"),
                    ("evento", "Fino all'evento (durata fissa)"),
                    ("evento_daily", "Giornaliero – tariffa al giorno fino all'evento"),
                ],
                help_text="Per 'Giornaliero' il campo Prezzo contiene la tariffa al giorno (es. 0.20). Per tutti gli altri è il prezzo totale.",
                max_length=20,
                null=True,
            ),
        ),
        migrations.AlterField(
            model_name="alertplan",
            name="price",
            field=models.DecimalField(
                decimal_places=2,
                help_text="Prezzo totale del piano. Per il piano Giornaliero indica la tariffa per singolo giorno (es. 0.20).",
                max_digits=10,
            ),
        ),
        migrations.AlterField(
            model_name="alertplan",
            name="duration_days",
            field=models.IntegerField(
                blank=True,
                default=0,
                help_text="Numero di giorni di durata. Per il piano Giornaliero lasciare 0 (viene calcolato automaticamente dall'evento).",
            ),
        ),
    ]
