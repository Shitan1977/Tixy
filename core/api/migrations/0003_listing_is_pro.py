from django.db import migrations, models


def add_is_pro_if_missing(apps, schema_editor):
    Listing = apps.get_model("api", "Listing")
    table_name = Listing._meta.db_table

    with schema_editor.connection.cursor() as cursor:
        existing_columns = {
            column.name for column in schema_editor.connection.introspection.get_table_description(cursor, table_name)
        }

    if "is_pro" in existing_columns:
        return

    field = models.BooleanField(default=False)
    field.set_attributes_from_name("is_pro")
    schema_editor.add_field(Listing, field)


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0002_ticketupload_extracted_subitems"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunPython(add_is_pro_if_missing, migrations.RunPython.noop),
            ],
            state_operations=[
                migrations.AddField(
                    model_name="listing",
                    name="is_pro",
                    field=models.BooleanField(default=False),
                ),
            ],
        ),
    ]
