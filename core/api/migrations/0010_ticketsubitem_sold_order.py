import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0009_listing_seller_fee_percent"),
    ]

    operations = [
        migrations.AddField(
            model_name="ticketsubitem",
            name="sold_order",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="claimed_subitems",
                to="api.orderticket",
            ),
        ),
    ]
