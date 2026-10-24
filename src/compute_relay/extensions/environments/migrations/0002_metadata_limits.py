import json
from django.db import migrations


def backfill(apps, schema_editor):
    Usage = apps.get_model('broker', 'MetadataUsage')
    Spec = apps.get_model('broker_environments', 'EnvironmentSpec')
    Build = apps.get_model('broker_environments', 'EnvironmentBuild')
    for spec in Spec.objects.iterator(chunk_size=100):
        Usage.objects.get_or_create(project_id=spec.project_id, kind='environment_spec', object_id=str(spec.id),
            defaults={'size_bytes': len(json.dumps(spec.document).encode()) + 1024 + 512 * len(spec.document.get('requirements', []))})
    for build in Build.objects.select_related('spec').iterator(chunk_size=100):
        Usage.objects.get_or_create(project_id=build.spec.project_id, kind='environment_build', object_id=str(build.id),
                                    defaults={'size_bytes': 4096})


class Migration(migrations.Migration):
    dependencies = [('broker', '0002_metadatapool_attempt_lease_deadline_at_and_more'),
                    ('broker_environments', '0001_initial')]
    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]
