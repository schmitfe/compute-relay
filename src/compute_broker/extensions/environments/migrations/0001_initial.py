import django.db.models.deletion
import django.utils.timezone
import uuid
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('broker', '0006_job_extension_name'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='EnvironmentBuildPolicy',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('configuration', models.JSONField(default=dict)),
                ('revision', models.PositiveIntegerField(default=1)),
                ('updated_at', models.DateTimeField(default=django.utils.timezone.now)),
                ('project', models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name='environment_build_policy', to='broker.project')),
                ('updated_by', models.ForeignKey(null=True, on_delete=django.db.models.deletion.PROTECT, to=settings.AUTH_USER_MODEL)),
            ],
        ),
        migrations.CreateModel(
            name='EnvironmentSpec',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('document', models.JSONField()),
                ('sha256', models.CharField(max_length=64)),
                ('created_at', models.DateTimeField(default=django.utils.timezone.now)),
                ('base_environment', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to='broker.environment')),
                ('connection', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to='broker.connection')),
                ('project', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to='broker.project')),
            ],
        ),
        migrations.CreateModel(
            name='EnvironmentBuild',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('policy_revision', models.PositiveIntegerField()),
                ('policy_sha256', models.CharField(max_length=64)),
                ('state', models.CharField(default='draft', max_length=32)),
                ('receipt', models.JSONField(default=dict)),
                ('error_code', models.CharField(blank=True, max_length=80)),
                ('created_at', models.DateTimeField(default=django.utils.timezone.now)),
                ('attempt', models.OneToOneField(null=True, on_delete=django.db.models.deletion.PROTECT, related_name='environment_build', to='broker.attempt')),
                ('credential', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to='broker.projectcredential')),
                ('environment', models.OneToOneField(null=True, on_delete=django.db.models.deletion.PROTECT, related_name='build_provenance', to='broker.environment')),
                ('job', models.OneToOneField(null=True, on_delete=django.db.models.deletion.PROTECT, related_name='environment_build', to='broker.job')),
                ('policy', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to='broker_environments.environmentbuildpolicy')),
                ('spec', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='builds', to='broker_environments.environmentspec')),
            ],
        ),
        migrations.AddConstraint(
            model_name='environmentspec',
            constraint=models.UniqueConstraint(fields=('project', 'sha256'), name='environment_spec_project_digest'),
        ),
    ]
