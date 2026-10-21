"""Infer only unambiguous legacy recipe ownership; preserve recipe/package bytes."""

from uuid import UUID

from django.db import migrations


def bind_legacy_environments(apps, schema_editor):
    alias = schema_editor.connection.alias
    Environment = apps.get_model("broker", "Environment")
    Project = apps.get_model("broker", "Project")
    Connection = apps.get_model("broker", "Connection")
    Job = apps.get_model("broker", "Job")
    legacy = {str(row.id): row for row in Environment.objects.using(alias).filter(connection__isnull=True).iterator()}
    if not legacy:
        return
    evidence = {key: {"projects": set(), "connections": set(), "unsafe": False} for key in legacy}
    for environment_id, project_id in Project.environments.through.objects.using(alias).values_list("environment_id", "project_id").iterator():
        if str(environment_id) in evidence:
            evidence[str(environment_id)]["projects"].add(project_id)
    # Historic job targets are evidence too. Current project permissions alone
    # must not reclassify a formerly cross-cluster recipe as cluster-specific.
    for project_id, request in Job.objects.using(alias).values_list("project_id", "request").iterator(chunk_size=1000):
        if not isinstance(request, dict) or not isinstance(request.get("environment"), dict):
            continue
        try:
            key = str(UUID(str(request["environment"].get("id"))))
        except (ValueError, TypeError, AttributeError):
            continue
        if key not in evidence:
            continue
        record = evidence[key]
        record["projects"].add(project_id)
        try:
            record["connections"].add(UUID(str(request["connection_id"])))
        except (KeyError, ValueError, TypeError, AttributeError):
            record["unsafe"] = True
    project_connections = {}
    for project_id, connection_id in Project.connections.through.objects.using(alias).values_list("project_id", "connection_id").iterator():
        project_connections.setdefault(project_id, set()).add(connection_id)
    project_owners = dict(Project.objects.using(alias).values_list("id", "owner_id"))
    connection_owners = dict(Connection.objects.using(alias).values_list("id", "owner_id"))
    for key, environment in legacy.items():
        record = evidence[key]
        if record["unsafe"] or not record["projects"]:
            continue
        owners = {project_owners.get(project_id) for project_id in record["projects"]}
        connections = set(record["connections"])
        for project_id in record["projects"]:
            connections.update(project_connections.get(project_id, set()))
        if len(owners) != 1 or None in owners or len(connections) != 1:
            continue
        owner_id, connection_id = next(iter(owners)), next(iter(connections))
        if connection_owners.get(connection_id) != owner_id or environment.owner_id not in (None, owner_id):
            continue
        Environment.objects.using(alias).filter(id=environment.id, connection__isnull=True).update(
            connection_id=connection_id, owner_id=owner_id)


class Migration(migrations.Migration):
    dependencies = [("broker", "0004_projectcredential_execution_mode")]
    operations = [migrations.RunPython(bind_legacy_environments, reverse_code=migrations.RunPython.noop)]
