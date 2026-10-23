"""Legacy storage metadata is assigned only with unique project evidence."""

import os
from pathlib import Path
import subprocess
import sys


def test_forward_migration_preserves_and_quarantines_ambiguous_metadata(tmp_path):
    source = Path(__file__).resolve().parents[3] / "src"
    env = {key: value for key, value in os.environ.items()
        if not key.startswith("BROKER_") and key != "DJANGO_SETTINGS_MODULE"}
    env.update(BROKER_EXTENSIONS="storage", DJANGO_SETTINGS_MODULE="compute_broker.control.settings",
        PYTHONPATH=str(source), BROKER_DATA_DIR=str(tmp_path / "migration-data"),
        BROKER_DB_PATH=str(tmp_path / "migration.sqlite3"))
    # A disposable process owns the complete migration lifecycle. The data
    # assignment is intentionally irreversible: it must not guess which future
    # project assignments previously held null when restoring an old schema.
    program = r'''
from datetime import timedelta
from uuid import uuid4
import importlib
import django
django.setup()
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.exceptions import IrreversibleError
from django.utils import timezone

old = ('broker_storage', '0003_personal_storage_registration')
new = ('broker_storage', '0004_project_storage_targets')
executor = MigrationExecutor(connection)
executor.migrate([old])
apps = executor.loader.project_state([old]).apps
User = apps.get_model('broker', 'User')
Project = apps.get_model('broker', 'Project')
Worker = apps.get_model('broker', 'WorkerIdentity')
Connection = apps.get_model('broker', 'Connection')
Task = apps.get_model('broker', 'ConnectorTask')
Target = apps.get_model('broker_storage', 'StorageTarget')
Pool = apps.get_model('broker_storage', 'StorageCapacityPool')
Binding = apps.get_model('broker_storage', 'ProjectStorageBinding')
Estimate = apps.get_model('broker_storage', 'StorageEstimateReservation')
Registration = apps.get_model('broker_storage', 'StorageTargetRegistration')
Budget = apps.get_model('broker_storage', 'ProjectStorageBudget')
owner = User.objects.create(username='synthetic-storage-migration')
worker = Worker.objects.create(owner=owner, name='Synthetic worker', token_hash='a' * 64)
cluster = Connection.objects.create(owner=owner, worker=worker, name='Synthetic cluster', backend='fake',
    configuration={}, revision_sha256='b' * 64)
first = Project.objects.create(owner=owner, name='First project')
second = Project.objects.create(owner=owner, name='Second project')
first.connections.add(cluster)
second.connections.add(cluster)
pool = Pool.objects.create(owner=owner, connection=cluster, name='Shared physical capacity',
    probe_root='/preserved/shared', observation={'synthetic': 'preserve exactly'})
roles = ['work', 'environments', 'results', 'scratch']
targets = {}
for name in ('unique', 'ambiguous', 'held', 'released', 'conflict', 'unbound'):
    targets[name] = Target.objects.create(owner=owner, connection=cluster, pool=pool, name=name,
        root_alias=name, root='/preserved/' + name, roles=roles, capabilities={'formats': ['directory']},
        profile_revision_sha256='c' * 64, connection_revision_sha256='b' * 64)
Binding.objects.create(project=first, connection=cluster, work=targets['unique'], environments=targets['unique'],
    results=targets['ambiguous'], scratch=targets['conflict'])
Binding.objects.create(project=second, connection=cluster, work=targets['ambiguous'])
for name, project, state in (('held', second, 'held'), ('released', first, 'released'), ('conflict', second, 'released')):
    Estimate.objects.create(project=project, target=targets[name], reference=uuid4(), bytes_estimate=123,
        inodes_estimate=4, state=state)
registrations = {}
for name in ('unique', 'ambiguous', 'unbound', 'pending'):
    task = Task.objects.create(owner=owner, connection=cluster, kind='mkdir', state='succeeded',
        request={'path': '/preserved', 'name': name}, result={'ok': True, 'path': '/preserved/' + name},
        connection_revision='b' * 64, expires_at=timezone.now() + timedelta(minutes=5))
    registrations[name] = Registration.objects.create(owner=owner, connection=cluster, task=task,
        target=targets.get(name), name=name, root='/preserved/' + name, roles=roles,
        profile_revision_sha256='c' * 64, connection_revision_sha256='b' * 64)
budget = Budget.objects.create(project=first, bytes_budget=10000, inodes_budget=100,
    retained_bytes_estimate=12, retained_inodes_estimate=3, estimate_source='human_estimate')
target_snapshots = {row.id: {field: getattr(row, field) for field in ('owner_id', 'connection_id', 'pool_id',
    'name', 'root_alias', 'root', 'roles', 'capabilities', 'profile_revision_sha256', 'connection_revision_sha256')}
    for row in Target.objects.all()}
task_snapshots = {row.id: (row.request, row.result, row.state) for row in Task.objects.all()}
executor = MigrationExecutor(connection)
executor.migrate([new])
apps = executor.loader.project_state([new]).apps
Target = apps.get_model('broker_storage', 'StorageTarget')
Registration = apps.get_model('broker_storage', 'StorageTargetRegistration')
Binding = apps.get_model('broker_storage', 'ProjectStorageBinding')
Estimate = apps.get_model('broker_storage', 'StorageEstimateReservation')
Pool = apps.get_model('broker_storage', 'StorageCapacityPool')
Budget = apps.get_model('broker_storage', 'ProjectStorageBudget')
Task = apps.get_model('broker', 'ConnectorTask')
expected = {'unique': first.id, 'ambiguous': None, 'held': second.id, 'released': first.id,
    'conflict': None, 'unbound': None}
assert Target.objects.count() == len(expected)
for name, project_id in expected.items():
    current = Target.objects.get(id=targets[name].id)
    assert current.project_id == project_id, name
    assert all(getattr(current, field) == value for field, value in target_snapshots[current.id].items())
assert Registration.objects.get(id=registrations['unique'].id).project_id == first.id
for name in ('ambiguous', 'unbound', 'pending'):
    assert Registration.objects.get(id=registrations[name].id).project_id is None
assert all(row.data_id is None for row in Binding.objects.all())
assert Binding.objects.count() == 2 and Estimate.objects.count() == 3 and Registration.objects.count() == 4
assert all((row.request, row.result, row.state) == task_snapshots[row.id] for row in Task.objects.all())
preserved = Budget.objects.get(project_id=first.id)
assert (preserved.bytes_budget, preserved.inodes_budget, preserved.retained_bytes_estimate,
    preserved.retained_inodes_estimate, preserved.estimate_source) == (10000, 100, 12, 3, 'human_estimate')
assert Pool.objects.get(id=pool.id).observation == {'synthetic': 'preserve exactly'}
# Rerunning only the conservative backfill cannot assign the ambiguous/unbound
# rows or change the already assigned project metadata.
migration = importlib.import_module('compute_broker.extensions.storage.migrations.0004_project_storage_targets')
with connection.schema_editor() as editor:
    migration.assign_unambiguous_projects(apps, editor)
assert {row.name: row.project_id for row in Target.objects.all()} == expected
with connection.cursor() as cursor:
    constraints = connection.introspection.get_constraints(cursor, Target._meta.db_table)
assert constraints['storage_project_target_once']['columns'] == ['project_id', 'connection_id',
    'root_alias', 'profile_revision_sha256', 'connection_revision_sha256']
assert 'storage_target_binding_once' not in constraints
assert not migration.Migration.operations[-1].reversible
print('project storage migration verified')
'''
    result = subprocess.run([sys.executable, "-c", program], env=env, capture_output=True,
        text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "project storage migration verified"
