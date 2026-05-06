from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="ticketupload",
            name="extracted_subitems",
            field=models.JSONField(blank=True, default=list),
        ),
    ]
