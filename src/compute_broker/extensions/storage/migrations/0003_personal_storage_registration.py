import django.db.models.deletion
import django.utils.timezone
import uuid
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("broker", "0015_cluster_checks"),
        ("broker_storage", "0002_storage_observation_leases"),
    ]

    operations = [
        migrations.CreateModel(
            name="StorageTargetRegistration",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("name", models.CharField(max_length=100)),
                ("root", models.CharField(max_length=240)),
                ("roles", models.JSONField(default=list)),
                ("profile_revision_sha256", models.CharField(max_length=64)),
                ("connection_revision_sha256", models.CharField(max_length=64)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("owner", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to="broker.user")),
                ("connection", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to="broker.connection")),
                ("task", models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, to="broker.connectortask")),
                ("target", models.OneToOneField(null=True, on_delete=django.db.models.deletion.PROTECT,
                    related_name="personal_registration", to="broker_storage.storagetarget")),
            ],
        ),
    ]
