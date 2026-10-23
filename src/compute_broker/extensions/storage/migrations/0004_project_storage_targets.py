"""Assign only storage metadata with one unambiguous historical project scope."""

from collections import defaultdict

from django.db import migrations, models
import django.db.models.deletion


def assign_unambiguous_projects(apps, schema_editor):
    Target = apps.get_model("broker_storage", "StorageTarget")
    Registration = apps.get_model("broker_storage", "StorageTargetRegistration")
    Binding = apps.get_model("broker_storage", "ProjectStorageBinding")
    Estimate = apps.get_model("broker_storage", "StorageEstimateReservation")
    alias = schema_editor.connection.alias
    references = defaultdict(set)
    fields = ("work_id", "environments_id", "data_id", "results_id", "scratch_id")
    for row in Binding.objects.using(alias).values_list("project_id", *fields).iterator():
        for target_id in row[1:]:
            if target_id is not None:
                references[target_id].add(row[0])
    # Released estimates are still historical ownership evidence. Do not move a
    # target away from a project merely because its current hold was released.
    for target_id, project_id in Estimate.objects.using(alias).values_list("target_id", "project_id").iterator():
        references[target_id].add(project_id)
    for target in Target.objects.using(alias).filter(project__isnull=True).iterator():
        projects = references.get(target.id, set())
        if len(projects) != 1:
            continue
        project_id = next(iter(projects))
        Target.objects.using(alias).filter(id=target.id, project__isnull=True).update(project_id=project_id)
        Registration.objects.using(alias).filter(target_id=target.id, project__isnull=True).update(project_id=project_id)


class Migration(migrations.Migration):
    dependencies = [("broker_storage", "0003_personal_storage_registration")]

    operations = [
        migrations.AddField(model_name="storagetarget", name="project", field=models.ForeignKey(
            null=True, blank=True, on_delete=django.db.models.deletion.PROTECT, to="broker.project")),
        migrations.AddField(model_name="storagetargetregistration", name="project", field=models.ForeignKey(
            null=True, blank=True, on_delete=django.db.models.deletion.PROTECT, to="broker.project")),
        migrations.AddField(model_name="projectstoragebinding", name="data", field=models.ForeignKey(
            null=True, blank=True, on_delete=django.db.models.deletion.PROTECT, related_name="data_bindings", to="broker_storage.storagetarget")),
        migrations.RemoveConstraint(model_name="storagetarget", name="storage_target_binding_once"),
        migrations.AddConstraint(model_name="storagetarget", constraint=models.UniqueConstraint(
            fields=("project", "connection", "root_alias", "profile_revision_sha256", "connection_revision_sha256"),
            name="storage_project_target_once")),
        migrations.RunPython(assign_unambiguous_projects),
    ]
