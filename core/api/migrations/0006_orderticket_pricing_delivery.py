from decimal import Decimal

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0005_alertplan_periodo"),
    ]

    operations = [
        migrations.AddField(
            model_name="orderticket",
            name="commission",
            field=models.DecimalField(decimal_places=2, default=Decimal("0.00"), max_digits=10),
        ),
        migrations.AddField(
            model_name="orderticket",
            name="change_name_fee",
            field=models.DecimalField(decimal_places=2, default=Decimal("0.00"), max_digits=10),
        ),
        migrations.AddField(
            model_name="orderticket",
            name="final_total",
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                help_text="Totale da addebitare: subtotale + commissione + cambio nominativo",
                max_digits=10,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="orderticket",
            name="delivered_ticket",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="delivered_orders",
                to="api.biglietto",
            ),
        ),
    ]
