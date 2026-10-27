"""Opt-in, operator-installed extensions with a small core lifecycle contract.

Names come from protected installation settings, never from an agent-controlled
import path. Bundled extensions can be split into distributions later without
moving their models or business logic into the core broker app.
"""

from importlib import import_module
import os

EXTENSIONS = {
    "storage": ("compute_relay.extensions.storage", "StorageConfig"),
    "environments": ("compute_relay.extensions.environments", "EnvironmentsConfig"),
    "workflows": ("compute_relay.extensions.workflows", "WorkflowsConfig"),
    "investigations": ("compute_relay.extensions.investigations", "InvestigationsConfig"),
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
        from compute_relay.control.errors import BrokerError
        raise BrokerError("EXTENSION_DISABLED", "This optional extension is disabled.", 409)
    return import_module(EXTENSIONS[name][0] + ".services")


def environment_details(environment, project):
    """Optional observation metadata without coupling core to extension models."""
    result = {}
    for name in control_names():
        handler = getattr(services_for(name), "environment_summary", None)
        if handler:
            result[name] = handler(environment, project)
    return result


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
        from compute_relay.control.errors import BrokerError
        raise BrokerError("REVISION_CHANGED", "The prepared extension binding changed.", 409)
    module = services_for(name)
    if event in {"validate_admission", "validate_dispatch"} and name not in job.project.connections.get(
            id=job.request["connection_id"]).worker.supported_extensions:
        from compute_relay.control.errors import BrokerError
        raise BrokerError("EXTENSION_UNAVAILABLE", "The assigned worker does not support this extension.", 409)
    handler = getattr(module, event, None)
    return handler(*args, **kwargs) if handler else None


def bind_job_context(job, credential, context, *, previous=None):
    """Fixed-registry metadata associations never select executable code."""
    if not isinstance(context, dict) or set(context) - set(control_names()):
        from compute_relay.control.errors import BrokerError
        raise BrokerError("EXTENSION_DISABLED", "Job context requires an enabled metadata extension.", 409)
    for name in control_names():
        module = services_for(name)
        if previous is not None:
            handler = getattr(module, "after_revision", None)
            if handler:
                handler(job, previous, credential)
        if name in context:
            handler = getattr(module, "bind_job", None)
            if handler is None:
                raise ValueError("This extension does not accept job context")
            handler(job, credential, context[name])


def filter_job_context(query, credential, context):
    if not isinstance(context, dict) or set(context) - set(control_names()):
        raise ValueError("Invalid job context filter")
    for name, value in context.items():
        handler = getattr(services_for(name), "filter_jobs", None)
        if handler is None:
            raise ValueError("Unsupported job context filter")
        query = handler(query, credential, value)
    return query


def validate_related_execution(job, credential):
    for name in control_names():
        handler = getattr(services_for(name), "validate_related_execution", None)
        if handler:
            handler(job, credential)


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
    return _worker_for_lease(worker, lease).execute(worker, lease)


def _worker_for_lease(worker, lease):
    """Share the enabled-module and bounded maintenance-lease import fence."""
    import json
    name = lease.get("extension")
    if name not in worker.extensions or name not in EXTENSIONS:
        raise ValueError("extension is not enabled on this worker")
    if len(json.dumps(lease).encode()) > 65536:
        raise ValueError("extension work lease exceeds limit")
    return import_module(EXTENSIONS[name][0] + ".worker")


def async_worker_names(names):
    """Advertise only installed extensions with a native async worker hook."""
    import inspect
    names = configured_names(",".join(names))
    for name in names:
        try:
            module = import_module(EXTENSIONS[name][0] + ".worker")
        except ImportError:
            raise ValueError("An enabled extension has no installed async worker hooks") from None
        if not any(inspect.iscoroutinefunction(getattr(module, hook, None))
                   for hook in ("execute_async", "after_observation_async")):
            raise ValueError("An enabled extension has no native async worker hooks")
    return names


async def execute_extension_work_async(worker, lease):
    """Async maintenance uses the same protected lease fence as sync workers."""
    module = _worker_for_lease(worker, lease)
    handler = getattr(module, "execute_async", None)
    if handler is None:
        raise ValueError("This extension has no async maintenance operation")
    return await handler(worker, lease)


def worker_after_observation(names, backend, native, manifest, contexts):
    name = manifest.get("extension_name", "")
    if not name or name not in names:
        return {}
    module = import_module(EXTENSIONS[name][0] + ".worker")
    handler = getattr(module, "after_observation", None)
    return {name: handler(backend, native, manifest, contexts.get(name, {}))} if handler else {}


async def worker_after_observation_async(names, backend, native, manifest, contexts):
    name = manifest.get("extension_name", "")
    if not name or name not in names or name not in EXTENSIONS:
        return {}
    module = import_module(EXTENSIONS[name][0] + ".worker")
    handler = getattr(module, "after_observation_async", None)
    return {name: await handler(backend, native, manifest, contexts.get(name, {}))} if handler else {}
