"""Owner-only remote setup through a leased personal connector."""
from test_support.unit_test_connector_setup import configuration_fields, lease, personal, reply

from datetime import timedelta
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from django.test import Client
from django.utils import timezone

from compute_relay.control.auth import token_hash
from compute_relay.control.models import (Connection, ConnectorEnrollment, ConnectorTask, ClusterRegistration,
    User, WorkerIdentity)
from compute_relay.control.services import digest
from compute_relay.worker.enrollment import profile_metadata, resolve_connection

pytestmark = pytest.mark.django_db




def api(context, path, payload):
    return Client().post(path, json.dumps(payload), content_type="application/json", HTTP_AUTHORIZATION="Bearer " + context.token)


def queue_browse(context, path=""):
    response = context.human.post(f"/clusters/{context.connection.id}/directories/", {
        "path": path, "connection_revision": context.connection.revision_sha256})
    assert response.status_code == 302
    return ConnectorTask.objects.latest("created_at")






def test_directory_listing_is_queued_owned_and_read_only(personal):
    context = personal
    assert context.human.get(f"/clusters/{context.connection.id}/directories/").status_code == 200
    assert not ConnectorTask.objects.exists()
    queue_browse(context)
    task = lease(context)
    assert task["kind"] == "browse" and task["request"] == {"path": ""}
    assert reply(context, task).status_code == 200
    assert reply(context, task).status_code == 200
    page = context.human.get(f"/clusters/{context.connection.id}/directories/?task={task['id']}")
    assert b"science/" in page.content and b"Use this working directory" in page.content


@pytest.mark.parametrize("superuser", [False, True])
def test_other_users_cannot_browse_or_edit_personal_configuration(personal, superuser):
    other = User.objects.create_user(username="other-owner", is_superuser=superuser)
    client = Client();client.force_login(other)
    for endpoint in ("directories", "configuration"):
        assert client.get(f"/clusters/{personal.connection.id}/{endpoint}/").status_code == 404
    assert not ConnectorTask.objects.exists()


def test_agent_bearer_is_not_a_human_setup_session(personal):
    response = Client().post(f"/clusters/{personal.connection.id}/directories/", {"path": "/"},
        HTTP_AUTHORIZATION="Bearer cbp_synthetic-agent")
    assert response.status_code == 403 and not ConnectorTask.objects.exists()


def test_directory_mutation_requires_csrf(personal):
    client = Client(enforce_csrf_checks=True);client.force_login(personal.owner)
    assert client.post(f"/clusters/{personal.connection.id}/directories/", {"path": "/"}).status_code == 403


@pytest.mark.parametrize("path", ["relative", "/tmp/../etc", "/tmp;touch", "/tmp//path"])
def test_invalid_directory_paths_never_reach_worker(personal, path):
    response = personal.human.post(f"/clusters/{personal.connection.id}/directories/", {
        "path": path, "connection_revision": personal.connection.revision_sha256})
    assert response.status_code == 400 and not ConnectorTask.objects.exists()


def test_stale_owner_request_does_not_queue(personal):
    response = personal.human.post(f"/clusters/{personal.connection.id}/directories/", {"connection_revision": "a" * 64})
    assert response.status_code == 409 and not ConnectorTask.objects.exists()


def test_setup_requires_a_current_connector_handshake(personal):
    personal.worker.setup_ready = False;personal.worker.save()
    response = personal.human.post(f"/clusters/{personal.connection.id}/directories/", {
        "connection_revision": personal.connection.revision_sha256})
    assert response.status_code == 409 and b"Start" in response.content


def test_revised_or_expired_task_cannot_change_configuration(personal):
    task = queue_browse(personal)
    current = lease(personal)
    task.expires_at = timezone.now() - timedelta(seconds=1);task.save()
    assert reply(personal, current).status_code == 409


def test_worker_cannot_report_someone_elses_task(personal):
    queue_browse(personal)
    current = lease(personal)
    other = WorkerIdentity.objects.create(owner=personal.owner, name="Another", token_hash=token_hash("cbw_other"))
    response = Client().post(f"/internal/v1/connectors/tasks/{current['id']}/report", json.dumps({
        "connection_id": str(personal.connection.id)}), content_type="application/json", HTTP_AUTHORIZATION="Bearer cbw_other")
    assert response.status_code == 404


def test_worker_hello_defers_new_jobs_until_post_login_confirmation(personal):
    response = api(personal, "/internal/v1/connectors/tasks", {"connection_id": str(personal.connection.id),
        "hello": True, "protocol": "personal-connector/v1", "needs_discovery": True})
    assert response.status_code == 200
    personal.connection.refresh_from_db()
    assert not personal.connection.setup_complete and not personal.connection.configuration_confirmed
    assert personal.connection.discovery_state == "queued"


def test_directory_results_are_escaped(personal):
    queue_browse(personal)
    current = lease(personal)
    result = {"ok": True, "path": "/home/researcher", "entries": [{"name": "<script>bad</script>", "path": "/home/researcher/test"}]}
    assert reply(personal, current, result=result).status_code == 200
    page = personal.human.get(f"/clusters/{personal.connection.id}/directories/?task={current['id']}")
    assert b"&lt;script&gt;bad&lt;/script&gt;" in page.content and b"<script>bad</script>" not in page.content




def test_human_configuration_requires_worker_ack_and_pins_profile(personal):
    context = personal
    response = context.human.post(f"/clusters/{context.connection.id}/configuration/", configuration_fields(context, maximum_cpus=4))
    assert response.status_code == 302
    context.connection.refresh_from_db()
    assert not context.connection.setup_complete
    task = lease(context)
    assert task["kind"] == "configure" and task["request"]["site_profile"]["maximum_cpus"] == 4
    protected = {**context.protected, "remote_root_prefixes": [task["request"]["remote_root"]],
        "site_profile": task["request"]["site_profile"], "profile_revision": 2}
    context.worker.profiles = [profile_metadata("personal-test", protected)];context.worker.save()
    config = resolve_connection({**context.connection.configuration, "remote_root": task["request"]["remote_root"]},
        {"personal-test": protected}, refresh=True)[1]
    result = {"ok": True, "path": task["request"]["remote_root"], "writable": True, "exists": False}
    assert reply(context, task, result=result, configuration=config).status_code == 200
    context.connection.refresh_from_db();context.registration.refresh_from_db()
    assert context.connection.configuration["site_profile"]["maximum_cpus"] == 4
    assert context.connection.configuration_confirmed and context.connection.setup_complete
    assert context.registration.requested_root == "/scratch/researcher/jobs"
    assert reply(context, task, result=result, configuration=config).status_code == 200


def test_personal_cluster_automatic_execution_is_owner_opt_in_and_waits_for_worker_ack(personal):
    context = personal
    page = context.human.get(f"/clusters/{context.connection.id}/configuration/")
    assert b'name="allow_automatic_execution"' in page.content
    assert b"removes only this cluster" in page.content
    response = context.human.post(f"/clusters/{context.connection.id}/configuration/",
        configuration_fields(context, allow_automatic_execution="on"))
    assert response.status_code == 302
    context.connection.refresh_from_db()
    assert context.connection.configuration["site_profile"]["automatic_execution_allowed"] is False
    assert context.connection.setup_complete is False
    task = lease(context)
    assert task["request"]["site_profile"]["automatic_execution_allowed"] is True
    protected = {**context.protected, "remote_root_prefixes": [task["request"]["remote_root"]],
        "site_profile": task["request"]["site_profile"], "profile_revision": 2}
    context.worker.profiles = [profile_metadata("personal-test", protected)]
    context.worker.save(update_fields=["profiles"])
    config = resolve_connection({**context.connection.configuration, "remote_root": task["request"]["remote_root"]},
        {"personal-test": protected}, refresh=True)[1]
    assert reply(context, task, result={"ok": True, "path": task["request"]["remote_root"], "writable": True},
        configuration=config).status_code == 200
    context.connection.refresh_from_db()
    assert context.connection.configuration["site_profile"]["automatic_execution_allowed"] is True
    assert context.connection.setup_complete is True


def test_personal_confirmation_checkbox_cannot_reconfigure_a_managed_worker_profile(personal):
    ConnectorEnrollment.objects.filter(worker=personal.worker).delete()
    response = personal.human.post(f"/clusters/{personal.connection.id}/configuration/",
        configuration_fields(personal, allow_automatic_execution="on"))
    assert response.status_code == 409 and not ConnectorTask.objects.exists()
    personal.connection.refresh_from_db()
    assert personal.connection.configuration["site_profile"]["automatic_execution_allowed"] is False


def test_module_avail_is_opt_in_and_acknowledged(personal):
    context = personal
    path = f"/clusters/{context.connection.id}/configuration/"
    data = configuration_fields(context, module_availability_enabled="on")
    assert context.human.post(path, data).status_code == 302
    task = lease(context)
    assert task["request"]["module_availability_enabled"] is True
    protected = {**context.protected, "remote_root_prefixes": [task["request"]["remote_root"]],
        "site_profile": task["request"]["site_profile"], "profile_revision": 2,
        "environment_discovery": {"module_inventory_mode": "auto", "module_availability_enabled": True}}
    context.worker.profiles = [profile_metadata("personal-test", protected)];context.worker.save()
    config = resolve_connection({**context.connection.configuration, "remote_root": task["request"]["remote_root"]},
        {"personal-test": protected}, refresh=True)[1]
    result = {"ok": True, "path": task["request"]["remote_root"], "writable": True}
    assert reply(context, task, result=result, configuration=config).status_code == 409
    assert reply(context, task, result={**result, "module_availability_enabled": True}, configuration=config).status_code == 200
    context.registration.refresh_from_db()
    assert context.registration.connector_settings["module_availability_enabled"] is True
    page = context.human.get(path)
    assert b'name="module_availability_enabled"' in page.content and b"checked" in page.content


def test_unconfirmed_discovery_does_not_enable_agent_execution(personal):
    context = personal
    context.connection.configuration_confirmed = False;context.connection.setup_complete = False;context.connection.save()
    response = context.human.get(f"/clusters/{context.connection.id}/configuration/")
    assert response.status_code == 200 and b"Confirm this configuration" in response.content


def test_bad_configuration_ack_cannot_widen_selected_limits(personal):
    context = personal
    response = context.human.post(f"/clusters/{context.connection.id}/configuration/", configuration_fields(context, maximum_cpus=4))
    assert response.status_code == 302
    task = lease(context)
    assert reply(context, task, configuration=context.connection.configuration,
        result={"ok": True, "path": "/scratch/researcher/jobs", "writable": True}).status_code == 409
    context.connection.refresh_from_db()
    assert not context.connection.setup_complete and context.connection.configuration["site_profile"]["maximum_cpus"] == 8


def test_no_configuration_before_discovery(personal):
    personal.connection.discovery_state = "queued";personal.connection.save()
    response = personal.human.post(f"/clusters/{personal.connection.id}/configuration/", configuration_fields(personal))
    assert response.status_code == 409 and not ConnectorTask.objects.exists()


def active_attempt(context):
    from compute_relay.control.models import Attempt, ExecutionRequest, Grant, Job, Project
    project = Project.objects.create(owner=context.owner, name="Active test")
    project.connections.add(context.connection)
    grant = Grant.objects.create(project=project, policy={})
    job = Job.objects.create(project=project, request={})
    execution = ExecutionRequest.objects.create(job=job)
    return Attempt.objects.create(job=job, execution_request=execution, connection=context.connection,
        grant=grant, reservation_cpu=1, reservation_gpu=0)


def test_configuration_is_refused_until_accounting_finishes(personal):
    active_attempt(personal)
    response = personal.human.post(f"/clusters/{personal.connection.id}/configuration/", configuration_fields(personal))
    assert response.status_code == 409 and not ConnectorTask.objects.exists()
    personal.connection.refresh_from_db();assert personal.connection.setup_complete


def test_upgrade_with_active_jobs_preserves_current_binding(personal):
    active_attempt(personal)
    response = api(personal, "/internal/v1/connectors/tasks", {"connection_id": str(personal.connection.id),
        "hello": True, "protocol": "personal-connector/v1", "needs_discovery": True})
    assert response.status_code == 200 and response.json()["defaults_deferred"] is True
    personal.connection.refresh_from_db()
    assert personal.connection.setup_complete and personal.connection.configuration_confirmed


def test_suspension_is_rechecked_under_setup_authority_fence(personal, monkeypatch):
    from compute_relay.control import connector_setup
    original = connector_setup.authenticated_connection
    def suspended(request, document):
        result = original(request, document)
        User.objects.filter(id=personal.owner.id).update(is_active=False)
        return result
    monkeypatch.setattr(connector_setup, "authenticated_connection", suspended)
    response = api(personal, "/internal/v1/connectors/tasks", {"connection_id": str(personal.connection.id)})
    assert response.status_code == 403


def test_discovery_during_configuration_cannot_reopen_admission(personal):
    from compute_relay.control.cluster_services import lease_discovery, report_discovery
    context = personal
    assert context.human.post(f"/clusters/{context.connection.id}/configuration/", configuration_fields(context)).status_code == 302
    context.connection.refresh_from_db();assert not context.connection.configuration_confirmed
    context.connection.discovery_state = "queued";context.connection.save()
    work = lease_discovery(context.worker)["lease"]
    report_discovery(context.worker, context.connection.id, {"lease_id": work["id"], "generation": work["generation"],
        "outcome": "discovered", "configuration": context.connection.configuration,
        "connection_revision_sha256": context.connection.revision_sha256, "discovery_snapshot": context.connection.discovery_snapshot})
    context.connection.refresh_from_db()
    assert not context.connection.setup_complete and not context.connection.configuration_confirmed


def test_new_folder_queues_one_owned_operation_and_opens_result(personal):
    context = personal
    response = context.human.post(f"/clusters/{context.connection.id}/directories/", {
        "action": "mkdir", "path": "/home/researcher", "name": "new-project",
        "connection_revision": context.connection.revision_sha256})
    assert response.status_code == 302
    work = lease(context)
    assert work["kind"] == "mkdir" and work["request"] == {"path": "/home/researcher", "name": "new-project"}
    result = {"ok": True, "path": "/home/researcher/new-project", "parent": "/home/researcher", "home": "/home/researcher",
        "entries": [], "writable": True, "truncated": False, "scanned_entries": 0, "created": True}
    assert reply(context, work, result=result).status_code == 200
    page = context.human.get(response.url)
    assert b"Folder created." in page.content and b"Use this working directory" in page.content
    context.connection.refresh_from_db()
    assert context.connection.configuration["remote_root"] == "/home/researcher/jobs"


@pytest.mark.parametrize("name", ["", ".", "..", "../escape", "nested/folder", "/absolute", "space here", "x;touch", "x" * 81])
def test_new_folder_names_are_single_safe_components(personal, name):
    response = personal.human.post(f"/clusters/{personal.connection.id}/directories/", {
        "action": "mkdir", "path": "/home/researcher", "name": name,
        "connection_revision": personal.connection.revision_sha256})
    assert response.status_code == 400 and not ConnectorTask.objects.exists()


def test_new_folder_requires_completed_setup_handshake(personal):
    personal.worker.setup_ready = False;personal.worker.save()
    response = personal.human.post(f"/clusters/{personal.connection.id}/directories/", {
        "action": "mkdir", "path": "/home/researcher", "name": "new-project",
        "connection_revision": personal.connection.revision_sha256})
    assert response.status_code == 409 and b"Start" in response.content and not ConnectorTask.objects.exists()


def test_existing_folder_result_is_not_described_as_new(personal):
    context = personal
    response = context.human.post(f"/clusters/{context.connection.id}/directories/", {
        "action": "mkdir", "path": "/home/researcher", "name": "existing",
        "connection_revision": context.connection.revision_sha256})
    work = lease(context)
    result = {"ok": True, "path": "/home/researcher/existing", "parent": "/home/researcher", "home": "/home/researcher",
        "entries": [], "writable": True, "truncated": False, "scanned_entries": 0, "created": False}
    assert reply(context, work, result=result).status_code == 200
    page = context.human.get(response.url)
    assert b"already existed" in page.content and b"Folder created." not in page.content


def test_hello_confirms_current_personal_connector_contract(personal):
    response = api(personal, "/internal/v1/connectors/tasks", {"connection_id": str(personal.connection.id),
        "hello": True, "protocol": "personal-connector/v1"})
    assert response.status_code == 200
    personal.worker.refresh_from_db()
    assert personal.worker.setup_ready


@pytest.mark.parametrize("protocol", [None, "personal-connector/v999", [], {}])
def test_unsupported_handshake_never_marks_connector_ready(personal, protocol):
    personal.worker.setup_ready = False
    personal.worker.save()
    response = api(personal, "/internal/v1/connectors/tasks", {"connection_id": str(personal.connection.id),
        "hello": True, "protocol": protocol})
    assert response.status_code == 409
    personal.worker.refresh_from_db()
    assert not personal.worker.setup_ready and not ConnectorTask.objects.exists()


def test_invalid_configuration_errors_follow_their_field_and_link_from_summary(personal):
    response = personal.human.post(f"/clusters/{personal.connection.id}/configuration/", configuration_fields(personal,
        maximum_cpus=0, maximum_memory_mib=0, remote_root="relative"))
    assert response.status_code == 200 and not ConnectorTask.objects.exists()
    body = response.content.decode()
    for name in ("maximum_cpus", "maximum_memory_mib", "remote_root"):
        assert f'href="#id_{name}"' in body
        assert f'id="id_{name}_error"' in body
        assert body.index(f'name="{name}"') < body.index(f'id="id_{name}_error"')
    assert 'aria-invalid="true"' in body and 'aria-describedby="id_remote_root_error"' in body


def inspection_result(**changes):
    return {"ok": True, "scope": "login_node", "verified_on_compute": False,
        "python": ["/opt/miniforge/bin/python3"], "conda_executable": "/opt/miniforge/bin/conda",
        "poetry_executable": "/opt/poetry/bin/poetry", "conda_status": "available", "truncated": False,
        "environments": [{"kind": "conda", "prefix": "/scratch/researcher/analysis",
            "interpreter": "/scratch/researcher/analysis/bin/python"}], **changes}


def queue_inspection(context, **changes):
    response = context.human.post(f"/clusters/{context.connection.id}/environments/new/", {
        "action": "inspect", "kind": "conda", "modules": "python3/miniforge3-24.3-py3.12",
        "connection_revision": context.connection.revision_sha256, **changes})
    assert response.status_code == 302, response.content
    return ConnectorTask.objects.latest("created_at")


def test_inspection_populates_cluster_bound_conda_dropdown_without_creating_environment(personal):
    from compute_relay.control.models import Environment
    context = personal
    path = f"/clusters/{context.connection.id}/environments/new/"
    assert context.human.get(path).status_code == 200
    assert not ConnectorTask.objects.exists()
    saved = queue_inspection(context)
    leased = lease(context)
    assert leased["kind"] == "environment_probe"
    assert leased["request"]["environment"] == {"modules": ["python3/miniforge3-24.3-py3.12"]}
    assert not Environment.objects.exists()
    assert reply(context, leased, result=inspection_result()).status_code == 200
    page = context.human.get(path, {"task": str(saved.id)})
    assert page.status_code == 200
    assert b"conda: /scratch/researcher/analysis" in page.content
    assert b"Compute-node availability is unverified" in page.content
    response = context.human.post(path, {"name": "Analysis", "kind": "conda", "interpreter": "/usr/bin/python3",
        "modules": "python3/miniforge3-24.3-py3.12", "inspection_task": str(saved.id),
        "detected_environment": "/scratch/researcher/analysis"})
    assert response.status_code == 302, response.content
    recipe = Environment.objects.get()
    assert recipe.connection == context.connection
    assert recipe.configuration == {"kind": "conda", "interpreter": "/scratch/researcher/analysis/bin/python",
        "modules": ["python3/miniforge3-24.3-py3.12"], "prefix": "/scratch/researcher/analysis",
        "conda_executable": "/opt/miniforge/bin/conda"}


def test_inspection_needs_human_post_csrf_current_revision_and_ready_connector(personal):
    context = personal
    path = f"/clusters/{context.connection.id}/environments/new/"
    data = {"action": "inspect", "kind": "system", "connection_revision": context.connection.revision_sha256}
    assert Client().post(path, data, HTTP_AUTHORIZATION="Bearer cbp_agent").status_code == 403
    browser = Client(enforce_csrf_checks=True); browser.force_login(context.owner)
    assert browser.post(path, data).status_code == 403
    context.worker.setup_ready = False; context.worker.save()
    assert context.human.post(path, data).status_code == 409
    context.worker.setup_ready = True; context.worker.save()
    assert context.human.post(path, {**data, "connection_revision": "obsolete"}).status_code == 409
    assert not ConnectorTask.objects.exists()


def test_inspection_stale_modules_or_other_owner_cannot_supply_detected_choices(personal):
    context = personal
    task = queue_inspection(context)
    assert reply(context, lease(context), result=inspection_result()).status_code == 200
    path = f"/clusters/{context.connection.id}/environments/new/"
    page = context.human.post(path, {"name": "Stale", "kind": "conda", "interpreter": "/usr/bin/python3",
        "modules": "different/module", "inspection_task": str(task.id), "detected_environment": "/scratch/researcher/analysis"})
    assert page.status_code == 200 and "discovered_modules" in page.context["form"].errors
    other = User.objects.create_user(username="other-environment-owner")
    client = Client(); client.force_login(other)
    assert client.get(path, {"task": str(task.id)}).status_code == 404
    context.connection.revision_sha256 = "d" * 64; context.connection.save()
    assert context.human.get(path, {"task": str(task.id)}).status_code == 409


@pytest.mark.parametrize("change", [
    {"verified_on_compute": True}, {"python": ["python3"]}, {"shell": "evil"},
    {"conda_executable": "/opt/bin/other"}, {"environments": [{"kind": "conda", "prefix": "/tmp/prefix", "interpreter": "/elsewhere/python"}]},
])
def test_invalid_inspection_worker_result_cannot_become_recipe_choices(personal, change):
    queue_inspection(personal)
    assert reply(personal, lease(personal), result=inspection_result(**change)).status_code == 400


@pytest.mark.parametrize("source", ["catalog", "manual"])
def test_default_recipe_can_inspect_anaconda_then_save_detected_conda_environment(personal, source):
    from compute_relay.control.models import Environment
    module = "python3/anaconda3-2024.06-py3.12"
    personal.connection.discovery_snapshot["software"] = {"modules": {"available": [module]}}
    personal.connection.save()
    selected = {"discovered_modules": [module], "modules": ""} if source == "catalog" else {"modules": module}
    saved = queue_inspection(personal, kind="system", interpreter="/usr/bin/python3", **selected)
    assert saved.request["environment"] == {"modules": [module]}
    assert saved.request["draft"]["kind"] == "modules"
    assert saved.request["draft"]["interpreter"] == "python3"
    assert reply(personal, lease(personal), result=inspection_result()).status_code == 200
    path = f"/clusters/{personal.connection.id}/environments/new/"
    page = personal.human.get(path, {"task": str(saved.id)})
    assert page.status_code == 200 and page.context["form"].initial["kind"] == "modules"
    response = personal.human.post(path, {"name": "Detected Anaconda", "kind": "modules", "interpreter": "python3",
        "inspection_task": str(saved.id), "detected_environment": "/scratch/researcher/analysis", **selected})
    assert response.status_code == 302, response.content
    recipe = Environment.objects.get()
    assert recipe.configuration["kind"] == "conda"
    assert recipe.configuration["modules"] == [module]
    assert recipe.configuration["interpreter"] == "/scratch/researcher/analysis/bin/python"
