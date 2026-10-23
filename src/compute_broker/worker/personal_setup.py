"""Async post-login setup for a personal worker; no controller dependencies."""

from copy import deepcopy
from importlib.resources import files
import os
import posixpath
from uuid import uuid4

from compute_broker.transports.async_openssh import AsyncOpenSSHTransport
from .async_runner import AsyncWorker
from .client import ControlError
from .connector_status import ConnectorStatus
from .enrollment import resolve_connection


async def remote_setup(session, operation, **arguments):
    connection = session.runtime_configuration()
    connection["command_timeout_seconds"] = 20
    transport = AsyncOpenSSHTransport(connection)
    transport.STDOUT_LIMIT = 128 * 1024
    source = files("compute_broker.remote").joinpath("connector_setup.py").read_text(encoding="utf-8")
    return await transport.call_helper(source, {"operation": operation, **arguments})


async def default_work_root(session):
    from .enrollment import _root
    result = await remote_setup(session, "identity")
    if result.get("ok") is not True:
        raise ValueError("Remote home directory could not be identified")
    return _root(posixpath.join(result["home"], ".compute-broker", "jobs"))


def save_state(directory, state):
    from .personal_connector import write_private
    temporary = directory / ("state-" + uuid4().hex + ".tmp")
    try:
        write_private(temporary, state)
        os.replace(temporary, directory / "state.json")
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


class PersonalWorker(AsyncWorker):
    def __init__(self, *args, state, session, state_directory, status=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.state, self.session, self.state_directory = state, session, state_directory
        self.status = status or ConnectorStatus(state)
        self._observed_attempts = {}
        self._pending_task = None
        self._defer_defaults = False
        self._setup_authorized = True
        self._pending_recovery_notified = None

    async def _execute(self, lease):
        operation = lease.get("operation")
        messages = {
            "submit": "Handling an approved job submission.",
            "reconcile": "Reconciling a previous job submission with Slurm.",
            "cancel": "Handling a job cancellation request.",
        }
        if operation == "observe":
            attempt = lease.get("attempt_id")
            if attempt not in self._observed_attempts:
                self.status.progress("Tracking a job's Slurm status and collecting its outputs when it finishes.")
                # Bounded local display bookkeeping; it never affects execution.
                if len(self._observed_attempts) >= 256:
                    self._observed_attempts.pop(next(iter(self._observed_attempts)))
                self._observed_attempts[attempt] = True
        elif operation in messages:
            self.status.progress(messages[operation])
        await super()._execute(lease)
        if operation in messages:
            self.status.progress("Job operation reported to the broker; waiting for more commands.")

    async def hello(self):
        result = await self.setup_request(hello=True)
        if result is None:
            return
        self._pending_task = result.get("task")
        self._defer_defaults = result.get("defaults_deferred", False)
        if not self.state.get("defaults_initialized") and not self._defer_defaults and self.state.get("module_discovery_mode") != "auto":
            updated = deepcopy(self.state)
            updated["module_discovery_mode"] = "auto"
            updated["profile_generation"] = self.state.get("profile_generation", 1) + 1
            self.persist(updated)
            await self.publish_state()

    async def setup_request(self, *, hello=False):
        document = {"connection_id": self.state["connection_id"]}
        if hello:
            document.update(hello=True, needs_discovery=not self.state.get("defaults_initialized", False),
                features=["personal_setup_v1", "personal_folders_v1", "personal_module_availability_v1"])
        if self.state.get("pending_configuration_task"):
            document["pending_task_id"] = self.state["pending_configuration_task"]
        try:
            result = await self.client.request("POST", "/internal/v1/connectors/tasks", document)
        except ControlError as error:
            if error.status != 403:
                raise
            if self._setup_authorized:
                self.status.problem("Cluster setup is paused until account verification is current. Existing jobs will still be tracked and accounted.")
            self._setup_authorized, self._defer_defaults = False, True
            return None
        self._setup_authorized = True
        pending = result.get("pending_configuration")
        if pending and pending["state"] == "succeeded":
            updated = deepcopy(self.state);updated.pop("pending_configuration_task", None)
            self.persist(updated)
        elif pending and pending["state"] == "failed" and self._pending_recovery_notified != document.get("pending_task_id"):
            self.status.problem("Cluster configuration needs confirmation again. Open Configure cluster in the portal and save it again; the previous change will not be rolled back automatically.")
            self._pending_recovery_notified = document.get("pending_task_id")
        return result

    async def publish_state(self):
        from .personal_connector import runtime_profile
        alias, profile = runtime_profile(self.state, self.session)
        self.credential_profiles = {alias: profile}
        self.connections.clear()
        await self.publish_profiles()

    def persist(self, new_state):
        save_state(self.state_directory, new_state)
        self.state.clear()
        self.state.update(new_state)

    async def discover(self, lease):
        from compute_broker.domain.cluster_configuration import derive_cluster_configuration
        from .enrollment import EnrollmentError
        report = {"lease_id": lease["id"], "generation": lease["generation"]}
        self.status.progress("Identifying Slurm partitions, resources and available software environments.")
        try:
            connection, public, revision = resolve_connection(lease["configuration"], self.credential_profiles, refresh=True)
            snapshot = await self.backend_factory(connection).discover()
            if not self.state.get("defaults_initialized") and not self._defer_defaults:
                updated = deepcopy(self.state)
                updated["site_profile"] = derive_cluster_configuration(snapshot, self.state.get("site_profile"))["site_profile"]
                updated["defaults_initialized"] = True
                updated["profile_generation"] = self.state.get("profile_generation", 1) + 1
                self.persist(updated)
                await self.publish_state()
                connection, public, revision = resolve_connection(lease["configuration"], self.credential_profiles, refresh=True)
            report.update(outcome="discovered", configuration=public, connection_revision_sha256=revision, discovery_snapshot=snapshot)
        except EnrollmentError as error:
            report.update(outcome="discovery_failed", error_code=error.code)
        except ControlError:
            raise
        except Exception:
            report.update(outcome="discovery_failed", error_code="DISCOVERY_UNAVAILABLE")
        await self.client.request("POST", f"/internal/v1/connections/{lease['connection_id']}/report", report)
        if report["outcome"] == "discovered":
            self.connections[lease["connection_id"]] = connection
            self.status.progress("Cluster discovery completed and reported to the broker.")
        else:
            self.status.progress("Cluster discovery failed; details are available in the portal.")

    async def setup_once(self):
        task = self._pending_task
        self._pending_task = None
        if task is None:
            if not self._setup_authorized:
                await self.hello()
                task = self._pending_task
                self._pending_task = None
            else:
                response = await self.setup_request()
                task = response.get("task") if response else None
        if task is None:
            return False
        descriptions = {"browse": "Listing folders for the portal's directory browser.",
            "mkdir": "Creating the folder requested in the portal.",
            "configure": "Validating and applying the cluster configuration requested in the portal."}
        self.status.progress(descriptions.get(task["kind"], "Handling a cluster setup request."))
        report = {"connection_id": self.state["connection_id"], "lease_id": task["lease_id"], "generation": task["generation"]}
        try:
            if task["kind"] == "browse":
                result = await remote_setup(self.session, "browse", path=task["request"]["path"])
            elif task["kind"] == "mkdir":
                if self.state.get("last_folder_task") == task["id"] and self.state.get("last_folder_receipt"):
                    receipt = self.state["last_folder_receipt"]
                    result = await remote_setup(self.session, "browse", path=receipt["path"])
                    result["created"] = receipt["created"]
                else:
                    result = await remote_setup(self.session, "mkdir", path=task["request"]["path"], name=task["request"]["name"])
                    if result.get("ok") is True:
                        updated = deepcopy(self.state)
                        updated["last_folder_task"] = task["id"]
                        updated["last_folder_receipt"] = {"path": result["path"], "created": result["created"]}
                        self.persist(updated)
            elif task["kind"] == "configure":
                from compute_broker.domain.cluster_configuration import validate_cluster_configuration
                validate_cluster_configuration(task["request"]["site_profile"], require_limits=True)
                if type(task["request"].get("module_availability_enabled", False)) is not bool:
                    raise ValueError("Invalid module listing setting")
                result = deepcopy(self.state["last_configuration_result"]) if (
                    self.state.get("last_configuration_task") == task["id"] and self.state.get("last_configuration_result")) else (
                        await remote_setup(self.session, "validate_root", path=task["request"]["remote_root"]))
                if result.get("ok") is True:
                    if self.state.get("last_configuration_task") != task["id"]:
                        updated = deepcopy(self.state)
                        updated["connection"]["remote_root"] = result["path"]
                        updated["site_profile"] = task["request"]["site_profile"]
                        updated["module_availability_enabled"] = task["request"].get("module_availability_enabled", False)
                        result["module_availability_enabled"] = updated["module_availability_enabled"]
                        updated["profile_generation"] = self.state.get("profile_generation", 1) + 1
                        updated["last_configuration_task"] = task["id"]
                        updated["last_configuration_result"] = result
                        updated["pending_configuration_task"] = task["id"]
                        self.persist(updated)
                    await self.publish_state()
                    alias = next(iter(self.credential_profiles))
                    selected = {"backend": "slurm", "credential_profile": alias,
                        "ssh": {key: self.state["connection"]["ssh"][key] for key in ("host", "user", "port")},
                        "remote_root": self.state["connection"]["remote_root"]}
                    report["configuration"] = resolve_connection(selected, self.credential_profiles, refresh=True)[1]
            else:
                raise ValueError("Unsupported personal setup operation")
            if result.get("ok") is True:
                report.update(outcome="succeeded", result=result)
            else:
                report.update(outcome="failed", error_code=result.get("error_code", "REMOTE_SETUP_UNAVAILABLE"))
        except ControlError:
            # A persisted profile or accepted acknowledgement must be replayed,
            # not reverted or replaced with a new scheduler operation.
            raise
        except Exception:
            report.update(outcome="failed", error_code="REMOTE_SETUP_UNAVAILABLE")
        try:
            await self.client.request("POST", f"/internal/v1/connectors/tasks/{task['id']}/report", report)
        except ControlError as error:
            if error.status != 403:
                raise
            self._setup_authorized, self._defer_defaults = False, True
            self.status.problem("Cluster setup confirmation is paused until account verification is current. Existing jobs will still be tracked and accounted.")
            return True
        if task["kind"] == "configure" and report["outcome"] == "succeeded":
            updated = deepcopy(self.state);updated.pop("pending_configuration_task", None)
            self.persist(updated)
        self.status.progress("Cluster setup request completed." if report["outcome"] == "succeeded"
            else "Cluster setup request failed; details are available in the portal.")
        return True
