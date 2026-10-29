"""Project/key simplification keeps explicit grants and secrets at their boundaries."""

from html.parser import HTMLParser
from importlib import import_module
from types import SimpleNamespace

from django.apps import apps
from django.db import connection

from test_support.unit_test_management import form_post, human, issued, key_data, management
from test_support.unit_test_management import SCOPES
from compute_relay.control.forms import KeyForm
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
