"""Hold prepared requests already awaiting approval without submitting any job."""

import django.db.models.deletion
from django.db import migrations, models


def backfill_pending_holds(apps, schema_editor):
    ExecutionRequest = apps.get_model("broker", "ExecutionRequest")
    Grant = apps.get_model("broker", "Grant")
    LedgerEntry = apps.get_model("broker", "LedgerEntry")
    alias = schema_editor.connection.alias
    safe_integer = 2**53 - 1
    maximum_balance = 2**63 - 1
    pending = ExecutionRequest.objects.using(alias).filter(state="awaiting_approval",
        reservation_state="none", attempt__isnull=True).select_related("job").order_by("job__project_id", "id")
    for execution in pending.iterator():
        manifest = execution.job.manifest
        if not manifest:
            # Historical unprepared fixture records have no execution package
            # or reservation to hold. Do not invent a resource allocation.
            continue
        reservation = manifest.get("reservation") if isinstance(manifest, dict) else None
        amounts = [reservation.get(key) for key in ("cpu_core_seconds", "gpu_device_seconds")] if isinstance(reservation, dict) else []
        if len(amounts) != 2 or any(type(value) is not int or not 0 <= value <= safe_integer for value in amounts):
            raise RuntimeError("A pending prepared execution has an invalid reservation. Repair that record before applying the approval-budget migration.")
        cpu, gpu = amounts
        grant = Grant.objects.using(alias).filter(project_id=execution.job.project_id).first()
        if grant is None:
            raise RuntimeError("A pending prepared execution has no project allowance. Repair that record before applying the approval-budget migration.")
        balances = (grant.reserved_cpu + cpu, grant.reserved_gpu + gpu)
        if any(not 0 <= value <= maximum_balance for value in balances):
            raise RuntimeError("Pending approval reservations exceed the supported balance range. Repair the allowance before applying the approval-budget migration.")
        execution.reservation_cpu, execution.reservation_gpu = cpu, gpu
        execution.reservation_state = "held"
        execution.save(using=alias, update_fields=["reservation_cpu", "reservation_gpu", "reservation_state"])
        LedgerEntry.objects.using(alias).create(grant=grant, execution_request=execution, action="hold", cpu=cpu, gpu=gpu,
            evidence={"source": "pending_approval_backfill", "migration": "0016_pending_approval_budget"})
        # Existing prepared claims remain held even if they now exceed the
        # allowance. New admissions must wait for a human decision or top-up.
        grant.reserved_cpu, grant.reserved_gpu = balances
        grant.save(using=alias, update_fields=["reserved_cpu", "reserved_gpu"])


class Migration(migrations.Migration):
    dependencies = [("broker", "0015_cluster_checks")]

    operations = [
        migrations.AddField(model_name="executionrequest", name="reservation_cpu", field=models.BigIntegerField(default=0)),
        migrations.AddField(model_name="executionrequest", name="reservation_gpu", field=models.BigIntegerField(default=0)),
        migrations.AddField(model_name="executionrequest", name="reservation_state", field=models.CharField(
            choices=[("none", "No approval reservation"), ("held", "Reserved for approval"),
                ("converted", "Converted to execution"), ("released", "Released")], default="none", max_length=20)),
        migrations.AddField(model_name="executionrequest", name="approval_requirements", field=models.JSONField(default=dict)),
        migrations.AddConstraint(model_name="executionrequest", constraint=models.CheckConstraint(
            condition=models.Q(reservation_cpu__gte=0, reservation_gpu__gte=0), name="nonnegative_approval_reservation")),
        migrations.AddConstraint(model_name="executionrequest", constraint=models.CheckConstraint(
            condition=models.Q(reservation_state__in=["none", "held", "converted", "released"]),
            name="known_approval_reservation_state")),
        migrations.AlterField(model_name="ledgerentry", name="attempt", field=models.ForeignKey(
            null=True, on_delete=django.db.models.deletion.PROTECT, to="broker.attempt")),
        migrations.AddField(model_name="ledgerentry", name="execution_request", field=models.ForeignKey(
            null=True, on_delete=django.db.models.deletion.PROTECT, related_name="budget_entries", to="broker.executionrequest")),
        migrations.AddConstraint(model_name="ledgerentry", constraint=models.UniqueConstraint(
            condition=models.Q(execution_request__isnull=False), fields=("execution_request", "action"),
            name="approval_ledger_action_once")),
        migrations.AddConstraint(model_name="ledgerentry", constraint=models.CheckConstraint(
            condition=(models.Q(attempt__isnull=False, execution_request__isnull=True)
                | models.Q(attempt__isnull=True, execution_request__isnull=False)), name="ledger_exactly_one_anchor")),
        # Reverting would destroy the only durable journal for outstanding
        # claims; restore an offline database backup for a code rollback.
        migrations.RunPython(backfill_pending_holds),
    ]
