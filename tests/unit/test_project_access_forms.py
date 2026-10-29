"""Project/key simplification keeps explicit grants and secrets at their boundaries."""

from html.parser import HTMLParser
from importlib import import_module
from types import SimpleNamespace

import pytest

from django.apps import apps
from django.db import connection

from test_support.unit_test_management import form_post, human, issued, key_data, management
from test_support.unit_test_management import SCOPES
from compute_relay.control.forms import KeyForm
from compute_relay.control.management import project_initial
from compute_relay.control.models import ProjectCredential
from tests.unit.test_management import project_data

class Inputs(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.checked = set()
        self.ids = set()
        self.error_links = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            self.ids.add(attrs["id"])
        if tag == "input" and "checked" in attrs:
            self.checked.add((attrs.get("name"), attrs.get("value")))
        if tag == "a" and attrs.get("href", "").startswith("#field_"):
            self.error_links.append(attrs["href"][1:])


def test_new_project_renders_owned_grants_together_without_checking_any(management):
    context = management
    page = human(context.owner).get("/projects/new/")
    assert page.status_code == 200
    html = page.content.decode()
    inputs = Inputs(html)
    assert not any(name in {"connections", "environments"} for name, _ in inputs.checked)
    for cluster in context.connections:
        assert f'data-environment-cluster="{cluster.id}"' in html
    assert "fieldset data-environment-cluster" in html
    assert "All cluster environments are listed" in html  # No-script instructions.
    assert "nothing is granted automatically" in html
    assert "Save cluster changes" not in html
    assert str(context.foreign.id) not in html
    for label in ("Execution policy", "Total project allowance", "Maximum CPUs per job", "Maximum memory per job"):
        assert label in html


def test_project_can_change_cluster_and_environment_in_one_post_without_javascript(management):
    context = management
    context.project.connections.set([context.connections[0]])
    context.project.environments.set([context.environment])
    response = form_post(human(context.owner), f"/projects/{context.project.id}/", project_data(context,
        connections=[str(context.connections[1].id)], environments=[str(context.secondary_environment.id)]))
    assert response.status_code == 302
    assert list(context.project.connections.values_list("id", flat=True)) == [context.connections[1].id]
    assert list(context.project.environments.values_list("id", flat=True)) == [context.secondary_environment.id]


@pytest.mark.parametrize("creating", [True, False])
def test_gpu_enabled_project_requires_explicit_types_in_the_form(management, creating):
    context = management
    previous_policy = context.grant.policy.copy()
    count = context.project.__class__.objects.count()
    path = "/projects/new/" if creating else f"/projects/{context.project.id}/"
    response = form_post(human(context.owner), path, project_data(context, max_gpus="1", allowed_gpu_types=[]))
    assert response.status_code == 200
    assert "allowed_gpu_types" in response.context["form"].errors
    assert b"None selected denies GPU jobs." in response.content
    assert b"Select at least one allowed GPU type" in response.content
    assert context.project.__class__.objects.count() == count
    context.grant.refresh_from_db()
    assert context.grant.policy == previous_policy


def test_cpu_only_project_accepts_empty_gpu_types(management):
    context = management
    response = form_post(human(context.owner), f"/projects/{context.project.id}/",
        project_data(context, max_gpus="0", gpu_hours="0", allowed_gpu_types=[]))
    assert response.status_code == 302
    context.grant.refresh_from_db()
    assert context.grant.policy["allowed_gpu_types"] == []
    assert context.grant.policy["per_job_limits"]["gpu_devices"] == 0
    assert context.grant.policy["allow_untyped_gpus"] is False


def test_gpu_type_permissions_are_unchecked_owned_cluster_checkboxes(management):
    context = management
    foreign = context.foreign
    foreign.discovery_snapshot = {"node_inventory_complete": True,
        "nodes": [{"partition": "debug", "gres": "gpu:private-type:1"}]}
    foreign.save(update_fields=["discovery_snapshot"])
    response = human(context.owner).get("/projects/new/")
    html = response.content.decode()
    form = response.context["form"]
    assert list(form.fields["allowed_gpu_types"].choices) == [("a100", "a100 · owned-0, owned-1")]
    assert 'type="checkbox" name="allowed_gpu_types"' in html
    assert '<fieldset class="form-field" id="field_allowed_gpu_types"' in html
    assert not any(name == "allowed_gpu_types" for name, _ in Inputs(html).checked)
    assert "private-type" not in html
    assert "Refresh cluster discovery if no GPU types are shown." in html


@pytest.mark.parametrize("selected,expected", [(["a100", "h200"], True), (["a100"], False)])
def test_gpu_all_and_partial_selection_persist_explicit_scheduler_choice(management, selected, expected):
    context = management
    second = context.connections[1]
    second.discovery_snapshot = {"node_inventory_complete": True,
        "nodes": [{"partition": "debug", "gres": "gpu:h200:1"}]}
    second.save(update_fields=["discovery_snapshot"])
    response = form_post(human(context.owner), f"/projects/{context.project.id}/",
        project_data(context, allowed_gpu_types=selected))
    assert response.status_code == 302
    context.grant.refresh_from_db()
    assert context.grant.policy["allowed_gpu_types"] == selected
    assert context.grant.policy["allow_untyped_gpus"] is expected


def test_unselected_cluster_types_do_not_prevent_all_selected_cluster_types(management):
    context = management
    second = context.connections[1]
    second.discovery_snapshot = {"node_inventory_complete": True,
        "nodes": [{"partition": "debug", "gres": "gpu:h200:1"}]}
    second.save(update_fields=["discovery_snapshot"])
    response = form_post(human(context.owner), f"/projects/{context.project.id}/", project_data(context,
        connections=[str(context.connections[0].id)], allowed_gpu_types=["a100"]))
    assert response.status_code == 302
    context.grant.refresh_from_db()
    assert context.grant.policy["allow_untyped_gpus"] is True


@pytest.mark.parametrize("snapshot", [{}, {"nodes": [{"partition": "debug", "gres": "gpu:a100:1"}]}])
def test_incomplete_gpu_inventory_retains_saved_types_without_generic_permission(management, snapshot):
    context = management
    for cluster in context.connections:
        cluster.discovery_snapshot = snapshot
        cluster.save(update_fields=["discovery_snapshot"])
    response = human(context.owner).get(f"/projects/{context.project.id}/")
    assert ("allowed_gpu_types", "a100") in Inputs(response.content.decode()).checked
    assert project_initial(context.project)["allowed_gpu_types"] == ["a100"]
    if not snapshot:
        assert b"Previously allowed; not currently discovered" in response.content
    response = form_post(human(context.owner), f"/projects/{context.project.id}/", project_data(context))
    assert response.status_code == 302
    context.grant.refresh_from_db()
    assert context.grant.policy["allowed_gpu_types"] == ["a100"]
    assert context.grant.policy["allow_untyped_gpus"] is False


def test_gpu_choices_reject_forged_types_and_csv_input(management):
    context = management
    previous_policy = context.grant.policy.copy()
    for selection in (["invented"], "a100,h200"):
        response = form_post(human(context.owner), f"/projects/{context.project.id}/",
            project_data(context, allowed_gpu_types=selection))
        assert response.status_code == 200
        assert "allowed_gpu_types" in response.context["form"].errors
    context.grant.refresh_from_db()
    assert context.grant.policy == previous_policy


def test_one_incomplete_allowed_cluster_blocks_generic_gpu_permission(management):
    context = management
    second = context.connections[1]
    second.discovery_snapshot = {}
    second.save(update_fields=["discovery_snapshot"])
    response = form_post(human(context.owner), f"/projects/{context.project.id}/", project_data(context))
    assert response.status_code == 302
    context.grant.refresh_from_db()
    assert context.grant.policy["allow_untyped_gpus"] is False


def test_discovery_changes_do_not_recompute_saved_gpu_permission_until_human_save(management):
    context = management
    owner = human(context.owner)
    response = form_post(owner, f"/projects/{context.project.id}/", project_data(context))
    assert response.status_code == 302
    context.grant.refresh_from_db()
    assert context.grant.policy["allow_untyped_gpus"] is True
    for cluster in context.connections:
        cluster.discovery_snapshot = {}
        cluster.save(update_fields=["discovery_snapshot"])
    assert owner.get(f"/projects/{context.project.id}/").status_code == 200
    context.grant.refresh_from_db()
    assert context.grant.policy["allow_untyped_gpus"] is True
    response = form_post(owner, f"/projects/{context.project.id}/", project_data(context))
    assert response.status_code == 302
    context.grant.refresh_from_db()
    assert context.grant.policy["allow_untyped_gpus"] is False


def test_no_script_unselected_cluster_recipe_fails_with_reachable_group_error(management):
    context = management
    before = set(context.project.environments.values_list("id", flat=True))
    response = form_post(human(context.owner), f"/projects/{context.project.id}/", project_data(context,
        connections=[str(context.connections[0].id)], environments=[str(context.secondary_environment.id)]))
    assert response.status_code == 200
    assert "environments" in response.context["form"].errors
    inputs = Inputs(response.content.decode())
    assert "field_environments" in inputs.error_links
    assert set(inputs.error_links) <= inputs.ids
    assert set(context.project.environments.values_list("id", flat=True)) == before


def test_key_has_two_review_choices_and_does_not_preselect_optional_permissions(management):
    context = management
    form = KeyForm(project=context.project)
    assert [value for value, _ in form.fields["execution_mode"].choices] == ["inherit", "approval_required"]
    assert set(form["scopes"].value()) == set(SCOPES)
    optional = {value for value, _ in form.fields["scopes"].choices} - set(SCOPES)
    assert optional
    assert not optional & set(form["scopes"].value())
    page = human(context.owner).get(f"/projects/{context.project.id}/keys/new/")
    html = page.content.decode()
    assert '<fieldset class="form-field" id="field_scopes"' in html
    assert "<legend>Permissions</legend>" in html
    assert "clusters.disabled = !restricted" in html


def test_removed_automatic_key_choice_is_rejected_on_create_and_edit(management):
    context = management
    owner = human(context.owner)
    before = ProjectCredential.objects.count()
    for path in (f"/projects/{context.project.id}/keys/new/", f"/projects/{context.project.id}/keys/{context.credential.id}/edit/"):
        response = form_post(owner, path, key_data(context, execution_mode="automatic"))
        assert response.status_code == 200
        assert "execution_mode" in response.context["form"].errors
    context.credential.refresh_from_db()
    assert context.credential.execution_mode == "inherit"
    assert ProjectCredential.objects.count() == before


def test_key_creation_handoff_keeps_secret_out_of_command_and_persistence(management):
    context = management
    response = form_post(human(context.owner), f"/projects/{context.project.id}/keys/new/", key_data(context))
    secret, _ = issued(response)
    command = response.context["init_command"]
    assert command == "compute-relay init-mcp --broker-url http://testserver"
    assert secret not in command
    assert "Paste the key into the hidden terminal prompt" in response.content.decode()
    assert "Keep it out of chat, command arguments and source files" in response.content.decode()
    assert secret.encode() not in human(context.owner).get(f"/projects/{context.project.id}/").content


def test_migration_normalizes_only_the_removed_equivalent_key_choice(management):
    context = management
    _, review = issued(form_post(human(context.owner), f"/projects/{context.project.id}/keys/new/",
                                key_data(context, execution_mode="approval_required")))
    ProjectCredential.objects.filter(id=context.credential.id).update(execution_mode="automatic")
    migration = import_module("compute_relay.control.migrations.0005_simplify_key_execution_mode")
    migration.follow_project_policy(apps, SimpleNamespace(connection=connection))
    context.credential.refresh_from_db()
    review.refresh_from_db()
    assert context.credential.execution_mode == "inherit"
    assert review.execution_mode == "approval_required"
