from django.db import migrations, models


def follow_project_policy(apps, schema_editor):
    apps.get_model("broker", "ProjectCredential").objects.using(schema_editor.connection.alias).filter(
        execution_mode="automatic").update(execution_mode="inherit")


class Migration(migrations.Migration):
    dependencies = [("broker", "0004_connection_availability_checked_at_and_more")]

    operations = [
        migrations.RunPython(follow_project_policy, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="projectcredential", name="execution_mode",
            field=models.CharField(max_length=20, default="inherit", choices=[
                ("inherit", "Follow project policy"), ("approval_required", "Require human confirmation")]),
        ),
    ]
