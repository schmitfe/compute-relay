"""Tests for environment recipes."""
from test_support.unit_test_management import form_post, human, management
from unittest.mock import patch
import pytest
from compute_relay.control import services
from compute_relay.control.models import Environment


@pytest.mark.parametrize("kind,details", [
    ("system", {"interpreter": "/usr/bin/python3"}),
    ("modules", {"interpreter": "python3", "modules": "python/3.12", "module_init": "/etc/profile.d/modules.sh"}),
    ("venv", {"interpreter": "/opt/venvs/test/bin/python", "prefix": "/opt/venvs/test"}),
    ("conda", {"interpreter": "/opt/conda/envs/test/bin/python", "prefix": "/opt/conda/envs/test", "conda_executable": "/opt/conda/bin/conda"}),
    ("poetry", {"interpreter": "/opt/poetry/test/bin/python", "prefix": "/opt/poetry/test"}),
])
def test_environment_recipe_is_stored_immutably_without_local_execution(management, kind, details):
    context = management
    data = {"name": f"existing {kind}", "connection": str(context.connections[0].id), "kind": kind, **details}
    with patch("subprocess.run", side_effect=AssertionError("environment enrollment must not execute software locally")):
        response = form_post(human(context.owner), f"/clusters/{context.connections[0].id}/environments/new/", data)
    assert response.status_code == 302
    environment = Environment.objects.get(name=data["name"])
    assert environment.owner_id == context.owner.id
    assert environment.connection_id == context.connections[0].id
    assert environment.revision_sha256 == services.digest(environment.configuration)
    assert environment.configuration["kind"] == kind


def test_modules_recipe_saves_exact_search_paths_in_immutable_digest(management):
    context = management
    data = {"name": "module search path recipe", "connection": str(context.connections[0].id),
            "kind": "modules", "interpreter": "python3", "modules": "Python/3.12\nSciPy/1.14",
            "module_init": "/etc/profile.d/z00_lmod.sh", "module_paths": "/opt/site/modules\n/opt/shared/modulefiles"}
    with patch("subprocess.run", side_effect=AssertionError("saving paths must not execute module hooks")):
        response = form_post(human(context.owner), f"/clusters/{context.connections[0].id}/environments/new/", data)
    assert response.status_code == 302
    environment = Environment.objects.get(name=data["name"])
    assert environment.configuration["module_paths"] == ["/opt/site/modules", "/opt/shared/modulefiles"]
    assert environment.configuration["modules"] == ["Python/3.12", "SciPy/1.14"]
    assert environment.revision_sha256 == services.digest(environment.configuration)
    changed = {**environment.configuration, "module_paths": ["/opt/other/modulefiles"]}
    assert services.digest(changed) != environment.revision_sha256


def test_modules_environment_form_prefills_paths_and_defaults_to_cluster_command(management):
    context = management
    connection = context.connections[0]
    connection.discovery_snapshot = {"software": {"modules": {
        "init_path": "/etc/profile.d/z00_lmod.sh", "module_paths": ["/opt/site/modules", "/opt/shared/modulefiles"]}}}
    connection.save(update_fields=["discovery_snapshot"])
    page = human(context.owner).get(f"/clusters/{connection.id}/environments/new/", {"kind": "modules"})
    assert page.status_code == 200
    form = page.context["form"]
    assert form.initial["interpreter"] == "python3"
    assert form.initial["module_init"] == ""
    assert "/etc/profile.d/z00_lmod.sh" in dict(form.fields["module_init"].choices)
    assert form.initial["module_paths"] == "/opt/site/modules\n/opt/shared/modulefiles"
    connection.owner = context.outsider
    connection.save(update_fields=["owner"])
    private = human(context.owner).get(f"/clusters/{connection.id}/environments/new/", {"kind": "modules"})
    assert private.status_code == 404
    assert b"/opt/site/modules" not in private.content


@pytest.mark.parametrize("kind,details", [
    ("system", {"interpreter": "/usr/bin/python3"}),
])
def test_nonmodule_environment_rejects_module_search_paths(management, kind, details):
    from compute_relay.control.forms import EnvironmentForm
    context = management
    form = EnvironmentForm({"name": "inconsistent environment", "connection": str(context.connections[0].id),
                            "kind": kind, **details, "module_paths": "/opt/site/modules"}, user=context.owner)
    assert not form.is_valid()
    assert "module_paths" in form.errors


@pytest.mark.parametrize("path", ["/opt/site/../escape", "/opt//modules", "/opt/modules/", "/opt/modules:/other", "$(touch injected)"])
def test_module_search_path_enrollment_rejects_noncanonical_or_shell_paths(management, path):
    context = management
    before = Environment.objects.count()
    response = form_post(human(context.owner), f"/clusters/{context.connections[0].id}/environments/new/", {
        "name": "bad module search path", "connection": str(context.connections[0].id), "kind": "modules",
        "interpreter": "python3", "modules": "Python/3.12", "module_init": "/etc/profile.d/modules.sh",
        "module_paths": path})
    assert response.status_code == 200
    assert response.context["form"].errors
    assert Environment.objects.count() == before
