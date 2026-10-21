"""The core gates new extension work independently of final accounting."""

from types import SimpleNamespace

import pytest
from django.db import transaction

from compute_broker import extensions
from compute_broker.control import services
from compute_broker.control.errors import BrokerError
from compute_broker.control.models import Job
from test_control import broker, budgeted, execution, prepared_job


@pytest.mark.parametrize("value", ["unknown", "environments,environments", "../storage", "storage:run", []])
def test_only_operator_selected_known_extension_names_are_accepted(value):
    with pytest.raises(ValueError):
        extensions.configured_names(value)


def test_extensions_are_opt_in(monkeypatch):
    monkeypatch.delenv("BROKER_EXTENSIONS", raising=False)
    assert extensions.configured_names() == ()
    assert extensions.application_configs(()) == []
    assert extensions.configured_names(" storage, environments ") == ("storage", "environments")


def extension_job(context, monkeypatch, *, worker_support=True):
    source = prepared_job(context)
    created, _ = services.create_job(context.credential, source.request)
    job = Job.objects.get(id=created["id"])
    job.extension_name = "environments"
    job.save(update_fields=["extension_name"])
    context.worker.supported_extensions = ["environments"] if worker_support else []
    context.worker.save(update_fields=["supported_extensions"])
    calls = []
    plugin = SimpleNamespace(validate_admission=lambda *args: calls.append("admit"),
                             after_admission=lambda *args: calls.append("admitted"),
                             validate_dispatch=lambda *args, **kwargs: calls.append("dispatch"))
    monkeypatch.setattr(extensions, "control_names", lambda: ("environments",))
    monkeypatch.setattr(extensions, "services_for", lambda name: plugin)
    services.prepare_job(job)
    job.refresh_from_db()
    return job, calls


def test_prepared_package_binds_extension_name_and_runs_admission_hooks(broker, monkeypatch):
    budgeted(broker)
    job, calls = extension_job(broker, monkeypatch)
    assert job.manifest["extension_name"] == "environments"
    request = execution(broker, job)
    assert request.attempt.id
    assert calls == ["admit", "admitted"]


def test_worker_must_advertise_extension_before_admission(broker, monkeypatch):
    budgeted(broker)
    job, calls = extension_job(broker, monkeypatch, worker_support=False)
    with pytest.raises(BrokerError, match="worker does not support"):
        execution(broker, job)
    broker.grant.refresh_from_db()
    assert broker.grant.reserved_cpu == 0
    assert not job.attempts.exists() and calls == []


def test_extension_binding_cannot_change_after_preparation(broker, monkeypatch):
    budgeted(broker)
    job, _ = extension_job(broker, monkeypatch)
    job.extension_name = "workflows"
    job.save(update_fields=["extension_name"])
    with pytest.raises(BrokerError, match="binding changed"):
        execution(broker, job)
    assert not job.attempts.exists()


def test_disabled_extension_blocks_undispatched_work_but_not_observation(broker, monkeypatch):
    budgeted(broker)
    job, calls = extension_job(broker, monkeypatch)
    request = execution(broker, job)
    attempt = request.attempt
    monkeypatch.setattr(extensions, "control_names", lambda: ())
    assert services.lease_work(broker.worker) == {"lease": None}
    # A scheduler job submitted before the extension was disabled still needs
    # its ordinary core observation/accounting lease.
    attempt.dispatch_started = True
    attempt.native_identity = {"job_id": "synthetic"}
    attempt.save(update_fields=["dispatch_started", "native_identity"])
    lease = services.lease_work(broker.worker)["lease"]
    assert lease["operation"] == "observe" and "extension_context" not in lease


def test_after_report_ignores_disabled_extension_without_importing_it(broker, monkeypatch):
    job, _ = extension_job(broker, monkeypatch)
    monkeypatch.setattr(extensions, "control_names", lambda: ())
    monkeypatch.setattr(extensions, "services_for", lambda _: pytest.fail("disabled extension imported"))
    extensions.after_report(SimpleNamespace(job=job), {})


def test_worker_never_loads_an_unconfigured_extension():
    assert extensions.worker_after_observation((), None, {}, {"extension_name": "environments"}, {}) == {}
