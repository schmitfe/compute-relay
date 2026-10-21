from django.db import migrations


def opaque_identifiers(apps, schema_editor):
    Credential = apps.get_model("broker", "ProjectCredential")
    for credential in Credential.objects.all().iterator():
        Credential.objects.filter(id=credential.id).update(prefix=str(credential.id)[:12])


class Migration(migrations.Migration):
    dependencies = [("broker", "0002_connection_discovery_error_and_more")]
    operations = [migrations.RunPython(opaque_identifiers, reverse_code=migrations.RunPython.noop)]
