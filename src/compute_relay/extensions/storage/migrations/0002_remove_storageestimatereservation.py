"""Remove unused advisory estimates; compute and artifact ledgers are separate."""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("broker_storage", "0001_initial")]

    operations = [migrations.DeleteModel(name="StorageEstimateReservation")]
