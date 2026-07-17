from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0006_orderticket_pricing_delivery"),
    ]

    operations = [
        migrations.AddField(
            model_name="orderticket",
            name="holder_names",
            field=models.JSONField(blank=True, null=True),
        ),
    ]
