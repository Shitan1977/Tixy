import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0003_listing_is_pro"),
    ]

    operations = [
        migrations.CreateModel(
            name="PushDevice",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("token", models.CharField(max_length=255, unique=True, verbose_name="Expo Push Token")),
                (
                    "platform",
                    models.CharField(
                        choices=[("android", "Android"), ("ios", "iOS"), ("unknown", "Unknown")],
                        default="unknown",
                        max_length=10,
                        verbose_name="Piattaforma",
                    ),
                ),
                ("device_id", models.CharField(blank=True, max_length=255, null=True, verbose_name="Device ID")),
                ("is_active", models.BooleanField(default=True, verbose_name="Attivo")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "utente",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="push_devices",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Utente",
                    ),
                ),
            ],
            options={
                "verbose_name": "Push Device",
                "verbose_name_plural": "Push Devices",
            },
        ),
        migrations.AddIndex(
            model_name="pushdevice",
            index=models.Index(fields=["utente", "is_active"], name="api_pushdev_utente_i_idx"),
        ),
    ]
