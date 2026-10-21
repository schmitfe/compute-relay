"""Opt-in, operator-installed extensions with a small core lifecycle contract.

Names come from protected installation settings, never from an agent-controlled
import path. Bundled extensions can be split into distributions later without
moving their models or business logic into the core broker app.
"""

from importlib import import_module
import os

EXTENSIONS = {
    "storage": ("compute_broker.extensions.storage", "StorageConfig"),
    "environments": ("compute_broker.extensions.environments", "EnvironmentsConfig"),
    "workflows": ("compute_broker.extensions.workflows", "WorkflowsConfig"),
    "transfers": ("compute_broker.extensions.transfers", "TransfersConfig"),
}


def configured_names(value=None):
    value = os.environ.get("BROKER_EXTENSIONS", "") if value is None else value
    if not isinstance(value, str):
        raise ValueError("BROKER_EXTENSIONS must be a comma-separated list")
    names = [name.strip() for name in value.split(",") if name.strip()]
    if len(names) != len(set(names)) or set(names) - EXTENSIONS.keys():
        raise ValueError("Unknown or duplicate broker extension")
    return tuple(names)


def application_configs(names):
    return [f"{EXTENSIONS[name][0]}.apps.{EXTENSIONS[name][1]}" for name in names]


def control_names():
    from django.conf import settings
    return tuple(settings.BROKER_EXTENSIONS)


def services_for(name):
    if name not in control_names():
        from compute_broker.control.errors import BrokerError
        raise BrokerError("EXTENSION_DISABLED", "This optional extension is disabled.", 409)
    return import_module(EXTENSIONS[name][0] + ".services")


def metadata_items(attribute):
    from importlib.util import find_spec
    result = []
    for name in control_names():
        module_name = EXTENSIONS[name][0] + ".metadata"
        if find_spec(module_name):
            result.extend(getattr(import_module(module_name), attribute, ()))
    return result


def job_hook(job, event, *args, **kwargs):
    name = job.extension_name
    if not name:
        return None
    if job.manifest and job.manifest.get("extension_name", "") != name:
        from compute_broker.control.errors import BrokerError
        raise BrokerError("REVISION_CHANGED", "The prepared extension binding changed.", 409)
    module = services_for(name)
    if event in {"validate_admission", "validate_dispatch"} and name not in job.project.connections.get(
            id=job.request["connection_id"]).worker.supported_extensions:
        from compute_broker.control.errors import BrokerError
        raise BrokerError("EXTENSION_UNAVAILABLE", "The assigned worker does not support this extension.", 409)
    handler = getattr(module, event, None)
    return handler(*args, **kwargs) if handler else None


def after_report(attempt, extension_data):
    """Called after the core transaction; a plugin cannot roll back settlement."""
    if attempt.job.extension_name in control_names():
        module = services_for(attempt.job.extension_name)
        handler = getattr(module, "after_report", None)
        if handler:
            handler(attempt, extension_data.get(attempt.job.extension_name, {}))


def before_lease(worker):
    """Bounded extension dispatch; core observation remains independently usable."""
    for name in control_names():
        if name in worker.supported_extensions:
            handler = getattr(services_for(name), "before_lease", None)
            if handler:
                handler(worker)


def lease_extension_work(worker):
    """Lease bounded maintenance work from installed, worker-supported plugins."""
    import json
    for name in control_names():
        if name not in worker.supported_extensions:
            continue
        handler = getattr(services_for(name), "lease_work", None)
        if not handler:
            continue
        lease = handler(worker)
        if lease is not None:
            if not isinstance(lease, dict) or set(lease) - {"id", "generation", "configuration", "context"}:
                raise ValueError("invalid extension work lease")
            if not {"id", "generation", "configuration", "context"} <= lease.keys():
                raise ValueError("incomplete extension work lease")
            lease = {**lease, "operation": "extension", "extension": name}
            if len(json.dumps(lease).encode()) > 65536:
                raise ValueError("extension work lease exceeds limit")
            return {"lease": lease}
    return None


def execute_extension_work(worker, lease):
    """Worker entry point: only operator-configured modules can be imported."""
    import json
    name = lease.get("extension")
    if name not in worker.extensions or name not in EXTENSIONS:
        raise ValueError("extension is not enabled on this worker")
    if len(json.dumps(lease).encode()) > 65536:
        raise ValueError("extension work lease exceeds limit")
    module = import_module(EXTENSIONS[name][0] + ".worker")
    return module.execute(worker, lease)


def worker_after_observation(names, backend, native, manifest, contexts):
    name = manifest.get("extension_name", "")
    if not name or name not in names:
        return {}
    module = import_module(EXTENSIONS[name][0] + ".worker")
    handler = getattr(module, "after_observation", None)
    return {name: handler(backend, native, manifest, contexts.get(name, {}))} if handler else {}
