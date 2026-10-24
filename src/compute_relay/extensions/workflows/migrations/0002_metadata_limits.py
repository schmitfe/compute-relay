import json
from django.db import migrations


def backfill(apps, schema_editor):
    Usage = apps.get_model('broker', 'MetadataUsage')
    Revision = apps.get_model('broker_workflows', 'WorkflowRevision')
    Run = apps.get_model('broker_workflows', 'WorkflowRun')
    for revision in Revision.objects.iterator(chunk_size=100):
        nodes = revision.document.get('nodes', [])
        Usage.objects.get_or_create(project_id=revision.project_id, kind='workflow_revision', object_id=str(revision.id),
            defaults={'size_bytes': len(json.dumps(revision.document).encode()) + 2048 +
                      512 * sum(len(node['request']['files']) for node in nodes)})
    for run in Run.objects.select_related('revision').iterator(chunk_size=100):
        Usage.objects.get_or_create(project_id=run.revision.project_id, kind='workflow_run', object_id=str(run.id),
            defaults={'size_bytes': 2048 + 2048 * len(run.revision.document.get('nodes', []))})


class Migration(migrations.Migration):
    dependencies = [('broker', '0002_metadatapool_attempt_lease_deadline_at_and_more'),
                    ('broker_workflows', '0001_initial')]
    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]
