"""Saved software catalogs and environment choices remain scoped to a cluster.

Every identity and snapshot is synthetic. Page/form tests cannot contact a login
node, run module hooks, or enqueue discovery through an ordinary GET request.
"""

from datetime import timedelta
from html.parser import HTMLParser
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import pytest
from django import forms
from django.test import Client
from django.utils import timezone

from compute_broker.control import services
from compute_broker.control.auth import token_hash
from compute_broker.control.forms import EnvironmentForm, ProjectForm
from compute_broker.control.models import Connection, Environment, Grant, Project, User, WorkerIdentity


@pytest.fixture
def catalog(db, monkeypatch):
    def no_subprocesses(*args, **kwargs):
        raise AssertionError("Catalog/UI tests must not execute subprocesses or SSH")
    monkeypatch.setattr("subprocess.run", no_subprocesses)
    monkeypatch.setattr("subprocess.Popen", no_subprocesses)
    owner = User.objects.create_user(username="catalog-owner", password=None)
    outsider = User.objects.create_user(username="private-catalog-owner", password=None)
    worker = WorkerIdentity.objects.create(owner=owner, name="catalog worker", token_hash=token_hash("synthetic-catalog-worker"))
    observed = "2026-10-21T09:37:00+00:00"

    def connection(name, modules, *, user=owner):
        snapshot = {"version": "synthetic scheduler", "observed_at": observed,
                    "nodes": [{"name": "cached-node-a", "partition": "debug", "cpus": 4,
                               "memory_mib": 8192, "gres": "gpu:test:1", "state": "MIXED"}],
                    "software": {"scope": "login_node", "verified_on_compute": False,
                                 "modules": {"status": "available", "available": modules,
                                             "init_path": "/etc/profile.d/z00_lmod.sh",
                                             "module_paths": ["/opt/site/modulefiles"], "truncated": False}}}
        config = {"backend": "fake", "ssh": {"host": "fixture.invalid", "user": "fixtureuser", "port": 22}}
        return Connection.objects.create(owner=user, worker=worker, name=name, backend="fake", configuration=config,
                                         revision_sha256=services.digest(config), discovery_state="ready",
                                         discovery_generation=7, discovery_snapshot=snapshot)

    first = connection("Atlas test cluster", ["Python/3.12", "CUDA/12.4", "SciPy-bundle/2024.05"])
    second = connection("Borealis test cluster", ["Python/3.11", "GROMACS/2025.1"])
    foreign = connection("Private outsider cluster", ["PrivateSuite/1.0"], user=outsider)

    def environment(name, cluster, *, user=owner):
        config = {"kind": "system", "interpreter": "/usr/bin/python3", "modules": []}
        return Environment.objects.create(owner=user, connection=cluster, name=name, configuration=config,
                                           revision_sha256=services.digest(config))

    first_env = environment("Atlas Python recipe", first)
    second_env = environment("Borealis Python recipe", second)
    foreign_env = environment("Private outsider Python recipe", foreign, user=outsider)
    legacy = environment("Unbound legacy Python recipe", None)
    project = Project.objects.create(owner=owner, name="Catalog scoped project")
    project.connections.add(first)
    project.environments.add(first_env)
    policy = {"schema_version": "1.0", "project_id": str(project.id), "revision": 1, "mode": "approval_required",
              "allowed_connection_ids": [str(first.id)], "allowed_environment_revision_sha256": [first_env.revision_sha256],
              "allowed_gpu_types": [], "max_active_jobs": 2, "max_total_jobs": 1000,
              "per_job_limits": {"cpu_cores": 4, "gpu_devices": 0, "memory_mib": 8192,
                                 "time_limit_seconds": 300, "maximum_charge_duration_seconds": 360},
              "allowance": {"cpu_core_seconds": 7200, "gpu_device_seconds": 0},
              "admission_expires_at": (timezone.now() + timedelta(days=3)).isoformat()}
    Grant.objects.create(project=project, policy=policy)
    return SimpleNamespace(owner=owner, outsider=outsider, worker=worker, first=first, second=second, foreign=foreign,
                           first_env=first_env, second_env=second_env, foreign_env=foreign_env, legacy=legacy,
                           project=project, observed=observed)


def human(user):
    client = Client(enforce_csrf_checks=True)
    client.force_login(user)
    client.get("/login/")
    return client


def post(client, path, data):
    return client.post(path, data, HTTP_X_CSRFTOKEN=client.cookies["csrftoken"].value)


def module_data(context, **changes):
    data = {"name": "Chosen catalog module recipe", "connection": str(context.first.id), "kind": "modules",
            "interpreter": "python3", "module_init": "/etc/profile.d/z00_lmod.sh", "module_paths": "/opt/site/modulefiles",
            "discovered_modules": ["Python/3.12"], "modules": ""}
    data.update(changes)
    return data


def project_data(context, **changes):
    data = {"name": context.project.name, "mode": "approval_required", "cpu_hours": "2", "gpu_hours": "0",
            "max_cpus": "4", "max_gpus": "0", "memory_mib": "8192", "wall_minutes": "5", "charge_minutes": "6",
            "max_active_jobs": "2", "max_total_jobs": "1000", "expires_at": (timezone.now() + timedelta(days=3)).isoformat(),
            "connections": [str(context.first.id)], "environments": [str(context.first_env.id)], "allowed_gpu_types": ""}
    data.update(changes)
    return data


def test_module_chooser_uses_only_selected_owned_clusters_saved_catalog(catalog):
    context = catalog
    page = human(context.owner).get(f"/clusters/{context.first.id}/environments/new/", {"connection": str(context.first.id), "kind": "modules"})
    assert page.status_code == 200
    field = page.context["form"].fields["discovered_modules"]
    assert isinstance(field.widget, forms.CheckboxSelectMultiple)
    assert set(dict(field.choices)) == {"Python/3.12", "CUDA/12.4", "SciPy-bundle/2024.05"}
    assert b"GROMACS/2025.1" not in page.content
    assert b"PrivateSuite/1.0" not in page.content


def test_cluster_catalog_link_preselects_one_exact_module_without_loading_it(catalog):
    context = catalog
    page = human(context.owner).get(f"/clusters/{context.first.id}/environments/new/", {
        "connection": str(context.first.id), "kind": "modules", "module": "CUDA/12.4"})
    assert page.status_code == 200
    assert page.context["form"].initial["discovered_modules"] == ["CUDA/12.4"]
    assert Environment.objects.filter(connection=context.first).count() == 1


@pytest.mark.parametrize("path", ["/environments/", "/environments/new/"])
def test_unscoped_legacy_pages_redirect_to_cluster_tree(catalog, path):
    page = human(catalog.owner).get(path, {"kind": "modules"})
    assert page.status_code == 302 and page.url == "/clusters/"


def test_selecting_a_second_owned_cluster_creates_its_own_catalog_recipe(catalog):
    context = catalog
    response = post(human(context.owner), f"/clusters/{context.second.id}/environments/new/", module_data(context,
        connection=str(context.second.id), discovered_modules=["GROMACS/2025.1"]))
    assert response.status_code == 302
    recipe = Environment.objects.get(name="Chosen catalog module recipe")
    assert recipe.connection_id == context.second.id
    assert recipe.configuration["modules"] == ["GROMACS/2025.1"]


def test_chosen_modules_and_explicit_advanced_modules_are_saved_without_local_execution(catalog):
    context = catalog
    data = module_data(context, discovered_modules=["CUDA/12.4", "Python/3.12"], modules="Python/3.12\ncustom/0.1")
    with patch("subprocess.run", side_effect=AssertionError("recipe forms must never execute module hooks")):
        response = post(human(context.owner), f"/clusters/{context.first.id}/environments/new/", data)
    assert response.status_code == 302
    recipe = Environment.objects.get(name=data["name"])
    assert recipe.connection_id == context.first.id
    assert recipe.configuration["modules"] == ["CUDA/12.4", "Python/3.12", "custom/0.1"]
    assert recipe.revision_sha256 == services.digest(recipe.configuration)


def test_module_recipe_defaults_to_cluster_command_without_explicit_initializer(catalog):
    context = catalog
    client = human(context.owner)
    path = f"/clusters/{context.first.id}/environments/new/"
    page = client.get(path, {"kind": "modules"})
    assert page.status_code == 200
    assert page.context["form"].initial["module_init"] == ""
    response = post(client, path, module_data(context, module_init=""))
    assert response.status_code == 302
    recipe = Environment.objects.get(name="Chosen catalog module recipe")
    assert recipe.configuration["module_init"] == ""


@pytest.mark.parametrize("chosen", ["GROMACS/2025.1", "PrivateSuite/1.0", "Python/3.12;touch /tmp/injected", "$(whoami)"])
def test_forged_or_other_cluster_catalog_selection_cannot_create_a_recipe(catalog, chosen):
    context = catalog
    count = Environment.objects.count()
    response = post(human(context.owner), f"/clusters/{context.first.id}/environments/new/", module_data(context, discovered_modules=[chosen]))
    assert response.status_code == 200
    assert response.context["form"].errors
    assert Environment.objects.count() == count


def test_untrusted_catalog_entries_are_escaped_and_cannot_become_executable_module_names(catalog):
    context = catalog
    attack = "<script>syntheticCatalogAttack()</script>"
    context.first.discovery_snapshot["software"]["modules"]["available"].append(attack)
    context.first.save(update_fields=["discovery_snapshot"])
    owner = human(context.owner)
    page = owner.get(f"/clusters/{context.first.id}/environments/new/", {"connection": str(context.first.id), "kind": "modules"})
    assert page.status_code == 200 and attack.encode() not in page.content
    before = Environment.objects.count()
    response = post(owner, f"/clusters/{context.first.id}/environments/new/", module_data(context, discovered_modules=[attack]))
    assert response.status_code == 200 and response.context["form"].errors
    assert Environment.objects.count() == before


def test_foreign_cluster_cannot_supply_module_choices_or_receive_a_new_recipe(catalog):
    context = catalog
    owner = human(context.owner)
    page = owner.get(f"/clusters/{context.foreign.id}/environments/new/", {"kind": "modules"})
    assert page.status_code == 404
    assert b"PrivateSuite/1.0" not in page.content
    before = Environment.objects.count()
    response = post(owner, f"/clusters/{context.first.id}/environments/new/", module_data(context, connection=str(context.foreign.id), discovered_modules=["PrivateSuite/1.0"]))
    assert response.status_code == 200 and "connection" in response.context["form"].errors
    assert Environment.objects.count() == before


def test_nonmodule_recipe_cannot_silently_accept_discovered_module_loads(catalog):
    context = catalog
    form = EnvironmentForm(module_data(context, kind="system", interpreter="/usr/bin/python3", module_init="", module_paths=""), user=context.owner)
    assert not form.is_valid()


class ElementText(HTMLParser):
    def __init__(self, tag):
        super().__init__(convert_charrefs=True)
        self.tag = tag
        self.active = None
        self.cells = []

    def handle_starttag(self, tag, attrs):
        if tag == self.tag:
            self.active = ""

    def handle_data(self, data):
        if self.active is not None:
            self.active += data

    def handle_endtag(self, tag):
        if tag == self.tag and self.active is not None:
            self.cells.append(self.active.strip().lower())
            self.active = None


def test_page_get_preserves_saved_snapshot_and_does_not_enqueue_or_execute_discovery(catalog):
    context = catalog
    original = {key: getattr(context.first, key) for key in (
        "discovery_state", "discovery_generation", "discovery_lease_id", "discovery_lease_expires_at", "discovery_snapshot")}
    owner = human(context.owner)
    with patch("subprocess.run", side_effect=AssertionError("page refresh must not start SSH")), \
         patch("compute_broker.control.cluster_services.lease_discovery", side_effect=AssertionError("GET must not acquire a discovery lease")):
        for path in (f"/clusters/{context.first.id}/", f"/clusters/{context.first.id}/environments/new/?kind=modules"):
            assert owner.get(path).status_code == 200
    context.first.refresh_from_db()
    assert {key: getattr(context.first, key) for key in original} == original


def test_cluster_resource_table_excludes_cached_state_and_explains_observation_and_async_refresh(catalog):
    context = catalog
    page = human(context.owner).get(f"/clusters/{context.first.id}/")
    assert page.status_code == 200
    html = page.content.decode()
    headings = ElementText("th")
    headings.feed(html)
    assert "state" not in headings.cells
    resource_table = html.split("<table", 1)[1].split("</table>", 1)[0]
    assert "MIXED" not in resource_table
    assert "cached-node-a" in resource_table
    lower = html.lower()
    assert "observed" in lower and "2026-10-21" in lower
    assert "saved" in lower or "cached" in lower
    assert "asynchronous" in lower


def test_canonical_environment_list_shows_only_its_cluster_recipes(catalog):
    context = catalog
    page = human(context.owner).get(f"/clusters/{context.first.id}/environments/")
    assert page.status_code == 200
    assert context.first_env.name.encode() in page.content
    for recipe in (context.second_env, context.foreign_env, context.legacy):
        assert recipe.name.encode() not in page.content
    other = human(context.owner).get(f"/clusters/{context.foreign.id}/environments/")
    assert other.status_code == 404
    assert context.foreign_env.name.encode() not in other.content


def test_cluster_inventory_and_overview_link_to_environment_subtree_without_global_nav(catalog):
    context = catalog
    owner = human(context.owner)
    index = owner.get("/clusters/")
    for connection in (context.first, context.second):
        assert f'/clusters/{connection.id}/environments/'.encode() in index.content
    page = owner.get(f"/clusters/{context.first.id}/")
    assert f'/clusters/{context.first.id}/environments/'.encode() in page.content
    for response in (index, page):
        assert b'href="/environments/"' not in response.content


def test_canonical_pages_have_no_cluster_picker_and_ignore_conflicting_query_cluster(catalog):
    context = catalog
    owner = human(context.owner)
    listing = owner.get(f"/clusters/{context.first.id}/environments/", {"connection": str(context.second.id)})
    creation = owner.get(f"/clusters/{context.first.id}/environments/new/", {"connection": str(context.second.id), "kind": "modules"})
    for page in (listing, creation):
        assert page.status_code == 200
        assert context.first.name.encode() in page.content
        assert context.second.name.encode() not in page.content
        assert b'id="recipe-cluster"' not in page.content
        assert b'id="environment-cluster"' not in page.content
    form = creation.context["form"]
    assert form.catalog_connection.id == context.first.id
    assert isinstance(form.fields["connection"].widget, forms.HiddenInput)
    assert set(form.fields["connection"].queryset.values_list("id", flat=True)) == {context.first.id}
    assert "GROMACS/2025.1" not in dict(form.fields["discovered_modules"].choices)


def test_canonical_creation_implicitly_binds_route_cluster_when_post_omits_connection(catalog):
    context = catalog
    data = module_data(context)
    del data["connection"]
    response = post(human(context.owner), f"/clusters/{context.first.id}/environments/new/", data)
    assert response.status_code == 302
    assert response.url == f"/clusters/{context.first.id}/environments/"
    assert Environment.objects.get(name=data["name"]).connection_id == context.first.id


@pytest.mark.parametrize("target", ["second", "foreign", "invalid", "duplicate"])
def test_canonical_creation_rejects_conflicting_or_ambiguous_post_cluster(catalog, target):
    context = catalog
    values = {"second": str(context.second.id), "foreign": str(context.foreign.id), "invalid": "not-a-uuid",
              "duplicate": [str(context.first.id), str(context.first.id)]}
    count = Environment.objects.count()
    response = post(human(context.owner), f"/clusters/{context.first.id}/environments/new/", module_data(context, connection=values[target]))
    assert response.status_code == 200 and "connection" in response.context["form"].errors
    assert response.context["form"].catalog_connection.id == context.first.id
    assert Environment.objects.count() == count


@pytest.mark.parametrize("suffix", ["", "new/"])
def test_legacy_contextual_get_redirects_owned_cluster_into_canonical_tree(catalog, suffix):
    context = catalog
    response = human(context.owner).get("/environments/" + suffix, {
        "connection": str(context.first.id), "kind": "modules", "module": "CUDA/12.4",
        "interpreter": "python3", "prefix": "/opt/venvs/one", "token": "SYNTHETIC_DROP_SENTINEL", "next": "https://outside.invalid"})
    assert response.status_code == 302
    location = urlsplit(response.url)
    assert not location.scheme and not location.netloc
    assert location.path == f"/clusters/{context.first.id}/environments/" + suffix
    expected = {"kind": ["modules"], "module": ["CUDA/12.4"], "interpreter": ["python3"], "prefix": ["/opt/venvs/one"]} if suffix else {}
    assert parse_qs(location.query) == expected
    assert "SYNTHETIC_DROP_SENTINEL" not in response.url


@pytest.mark.parametrize("path_kind", ["canonical_list", "canonical_create", "legacy_list", "legacy_create"])
def test_foreign_cluster_environment_routes_return_404(catalog, path_kind):
    context = catalog
    paths = {"canonical_list": f"/clusters/{context.foreign.id}/environments/",
             "canonical_create": f"/clusters/{context.foreign.id}/environments/new/",
             "legacy_list": "/environments/", "legacy_create": "/environments/new/"}
    owner = human(context.owner)
    response = owner.get(paths[path_kind], {"connection": str(context.foreign.id)})
    assert response.status_code == 404
    assert context.foreign.name.encode() not in response.content
    if path_kind == "canonical_create":
        assert post(owner, paths[path_kind], module_data(context, connection=str(context.foreign.id))).status_code == 404


def test_legacy_post_cannot_create_or_forward_a_recipe(catalog):
    context = catalog
    count = Environment.objects.count()
    response = post(human(context.owner), "/environments/new/", module_data(context))
    assert response.status_code == 405
    assert "Location" not in response
    assert Environment.objects.count() == count


def test_versioned_system_initializer_from_selected_cluster_snapshot_can_be_saved(catalog):
    context = catalog
    initializer = "/usr/share/lmod/8.7.47/init/bash"
    context.first.discovery_snapshot["software"]["modules"]["init_path"] = initializer
    context.first.save(update_fields=["discovery_snapshot"])
    owner = human(context.owner)
    page = owner.get(f"/clusters/{context.first.id}/environments/new/", {"kind": "modules"})
    assert initializer in dict(page.context["form"].fields["module_init"].choices)
    response = post(owner, f"/clusters/{context.first.id}/environments/new/", module_data(context, module_init=initializer))
    assert response.status_code == 302
    assert Environment.objects.get(name="Chosen catalog module recipe").configuration["module_init"] == initializer


@pytest.mark.parametrize("source", ["invalid_owned", "foreign"])
def test_invalid_or_foreign_snapshot_initializer_cannot_expand_selected_clusters_choices(catalog, source):
    context = catalog
    initializer = "/home/catalog/untrusted.sh" if source == "invalid_owned" else "/usr/share/lmod/9.9.99/init/bash"
    connection = context.first if source == "invalid_owned" else context.foreign
    connection.discovery_snapshot["software"]["modules"]["init_path"] = initializer
    connection.save(update_fields=["discovery_snapshot"])
    owner = human(context.owner)
    page = owner.get(f"/clusters/{context.first.id}/environments/new/", {"kind": "modules"})
    assert initializer not in dict(page.context["form"].fields["module_init"].choices)
    count = Environment.objects.count()
    response = post(owner, f"/clusters/{context.first.id}/environments/new/", module_data(context, module_init=initializer))
    assert response.status_code == 200 and "module_init" in response.context["form"].errors
    assert Environment.objects.count() == count


def test_new_project_form_excludes_foreign_and_unbound_environment_recipes(catalog):
    context = catalog
    form = ProjectForm(user=context.owner)
    selectable = set(form.fields["environments"].queryset.values_list("id", flat=True))
    assert selectable == {context.first_env.id, context.second_env.id}
    groups = list(form.fields["environments"].choices)
    assert {str(label) for label, _ in groups} == {context.first.name, context.second.name}


def test_existing_project_get_offers_only_environments_for_its_selected_clusters(catalog):
    context = catalog
    page = human(context.owner).get(f"/projects/{context.project.id}/")
    assert page.status_code == 200
    selectable = set(page.context["form"].fields["environments"].queryset.values_list("id", flat=True))
    assert selectable == {context.first_env.id}


@pytest.mark.parametrize("choice", ["second_env", "foreign_env", "legacy"])
def test_project_post_rejects_environment_from_unselected_foreign_or_unbound_cluster(catalog, choice):
    context = catalog
    recipe = getattr(context, choice)
    before = Grant.objects.get(project=context.project).policy
    response = post(human(context.owner), f"/projects/{context.project.id}/", project_data(context, environments=[str(recipe.id)]))
    assert response.status_code == 200
    assert "environments" in response.context["form"].errors
    assert set(context.project.environments.values_list("id", flat=True)) == {context.first_env.id}
    assert Grant.objects.get(project=context.project).policy == before


def test_project_post_can_select_environment_when_its_cluster_is_also_selected(catalog):
    context = catalog
    response = post(human(context.owner), f"/projects/{context.project.id}/", project_data(context,
        connections=[str(context.first.id), str(context.second.id)], environments=[str(context.second_env.id)]))
    assert response.status_code == 302
    assert set(context.project.environments.values_list("id", flat=True)) == {context.second_env.id}
