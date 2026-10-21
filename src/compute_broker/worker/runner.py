"""Polling execution worker with persistent intent fencing."""

from __future__ import annotations

import base64
import logging
import os
import time
from pathlib import Path
from uuid import UUID

from .client import ControlError, JsonClient, environment_token, strict_json
from .journal import Journal
from .enrollment import EnrollmentError, profile_metadata, resolve_connection


class Worker:
    def __init__(self, client, connections: dict, journal: Journal, backend_factory=None,
                 credential_profiles=None, extensions=None):
        self.client = client
        self.connections = connections
        self.journal = journal
        self.backend_factory = backend_factory or self._backend
        self.credential_profiles = credential_profiles or {}
        from compute_broker.extensions import configured_names
        self.extensions = configured_names(",".join(extensions)) if extensions is not None else configured_names()
        self._profiles_published_at = None

    @staticmethod
    def _backend(connection):
        from compute_broker.backends import FakeBackend, SlurmBackend
        kind = connection.get("backend", "slurm")
        if kind == "fake":
            return FakeBackend(connection)
        if kind == "slurm":
            return SlurmBackend(connection)
        raise ValueError("unsupported configured backend")

    def poll_once(self) -> bool:
        now = time.monotonic()
        if self._profiles_published_at is None or now - self._profiles_published_at >= 60:
            self.publish_profiles()
        response = self.client.request("POST", "/internal/v1/lease", {})
        lease = response.get("lease")
        if lease is None:
            return False
        self.execute(lease)
        return True

    def publish_profiles(self):
        profiles = []
        for alias, profile in sorted(self.credential_profiles.items()):
            try:
                profiles.append(profile_metadata(alias, profile))
            except EnrollmentError:
                # One unavailable connection must not block healthy targets.
                # No invalid profile is advertised and no secret is logged.
                logging.getLogger(__name__).warning("A credential profile is invalid and was not advertised")
        payload = {"profiles": profiles}
        if self.extensions:
            payload["extensions"] = list(self.extensions)
        self.client.request("POST", "/internal/v1/worker/profiles", payload)
        self._profiles_published_at = time.monotonic()

    def discover(self, lease):
        connection_id = str(UUID(lease["connection_id"]))
        report = {"lease_id": lease["id"], "generation": lease["generation"]}
        try:
            # Discovery may renew derived settings; the explicit host/user/port/
            # root/alias must still satisfy the current protected target scope.
            configuration, public, revision = resolve_connection(
                lease["configuration"], self.credential_profiles, refresh=True)
            snapshot = self.backend_factory(configuration).discover()
            report.update(outcome="discovered", configuration=public,
                          connection_revision_sha256=revision, discovery_snapshot=snapshot)
        except EnrollmentError as exc:
            report.update(outcome="discovery_failed", error_code=exc.code)
        except Exception:
            report.update(outcome="discovery_failed", error_code="DISCOVERY_UNAVAILABLE")
        self.client.request("POST", f"/internal/v1/connections/{connection_id}/report", report)
        if report["outcome"] == "discovered":
            self.connections[connection_id] = configuration

    def execute(self, lease):
        from compute_broker.backends import connection_revision_sha256, verify_package

        if lease.get("operation") == "discover":
            self.discover(lease)
            return
        if lease.get("operation") == "extension":
            from compute_broker.extensions import execute_extension_work
            execute_extension_work(self, lease)
            return
        attempt_id = str(UUID(lease["attempt_id"]))
        operation = lease["operation"]
        if operation not in {"submit", "reconcile", "observe", "cancel"}:
            raise ValueError("unsupported worker operation")
        public = lease.get("configuration", {})
        if public.get("credential_profile"):
            connection, _, _ = resolve_connection(public, self.credential_profiles)
        else:
            connection = self.connections[lease["connection_id"]]
        package = lease["prepared"]
        manifest, digest = package["manifest"], package["manifest_sha256"]
        if manifest["request"]["connection_id"] != lease["connection_id"]:
            raise ValueError("package connection mismatch")
        files = {}
        total = 0
        for path, encoded in package["files"].items():
            if len(encoded) > 140 * 1024 * 1024:
                raise ValueError("file exceeds worker limit")
            content = base64.b64decode(encoded, validate=True)
            total += len(content)
            if total > 256 * 1024 * 1024 or len(files) >= 110:
                raise ValueError("package exceeds worker limit")
            files[path] = content
        verify_package(manifest, digest, files)
        configured_revision = connection_revision_sha256(connection)
        if configured_revision != manifest["connection_revision_sha256"]:
            raise ValueError("configured connection revision changed")
        backend = self.backend_factory(connection)
        with self.journal.locked(attempt_id):
            previous = self.journal.read(attempt_id)
            if previous and previous["manifest_sha256"] != digest:
                raise ValueError("attempt package changed after intent")
            if operation in {"submit", "reconcile"}:
                result = self._submission(lease, backend, manifest, files, previous)
            elif operation == "cancel":
                native = lease.get("native_identity") or (previous or {}).get("native_identity")
                if not native:
                    result = {"outcome": "submission_uncertain"}
                else:
                    backend.cancel(native)
                    result = {"outcome": "cancel_requested", "native_identity": native}
            else:
                native = lease.get("native_identity") or (previous or {}).get("native_identity")
                if not native:
                    result = {"outcome": "submission_uncertain"}
                else:
                    observation = backend.observe(native)
                    result = {"outcome": "observed", "native_identity": native,
                              "observation": observation}
                    if observation.get("terminal"):
                        try:
                            from compute_broker.extensions import worker_after_observation
                            extension_data = worker_after_observation(self.extensions, backend, native, manifest,
                                lease.get("extension_context", {}))
                            if extension_data:
                                result["extension_data"] = extension_data
                        except Exception:
                            result["extension_data"] = {manifest.get("extension_name", ""): {"error": "FINALIZATION_UNAVAILABLE"}}
                        try:
                            result.update(self._collect(backend, native, manifest))
                        except Exception:
                            # Output retention is independent of trustworthy native
                            # accounting. Never strand a completed allocation's
                            # reservation because a result is unsafe or unavailable.
                            result.update(artifacts={}, error_code="OUTPUT_COLLECTION_FAILED")
            report = {"lease_id": lease["id"], "generation": lease["generation"], **result}
            self.client.request("POST", f"/internal/v1/attempts/{attempt_id}/report", report)

    def _submission(self, lease, backend, manifest, files, previous):
        attempt_id = str(UUID(lease["attempt_id"]))
        digest = lease["prepared"]["manifest_sha256"]
        if previous:
            if previous.get("outcome") in {"submitted", "rejected"}:
                return previous["result"]
            # A persisted intent means submission may have happened. Never retry it.
            result = backend.reconcile(attempt_id, manifest)
        elif lease["operation"] == "reconcile":
            result = backend.reconcile(attempt_id, manifest)
        else:
            confirmed = self.client.request(
                "POST", f"/internal/v1/attempts/{attempt_id}/dispatch",
                {"lease_id": lease["id"], "generation": lease["generation"],
                 "manifest_sha256": digest,
                 "connection_revision_sha256": manifest["connection_revision_sha256"]},
            )
            if not confirmed.get("authorized"):
                raise ControlError("DISPATCH_DENIED")
            self.journal.write(attempt_id, {"manifest_sha256": digest, "outcome": "intent"})
            try:
                result = backend.submit(attempt_id, manifest, files)
            except Exception:
                # Neither exception text nor an assumption of non-submission is safe.
                result = {"outcome": "submission_uncertain"}
        if result.get("outcome") not in {"submitted", "submission_uncertain", "rejected"}:
            result = {"outcome": "submission_uncertain"}
        # A remote error message is not safe service metadata.
        result = {key: value for key, value in result.items()
                  if key in {"outcome", "native_identity"}}
        record = {"manifest_sha256": digest, "outcome": result["outcome"], "result": result}
        if result.get("native_identity"):
            record["native_identity"] = result["native_identity"]
        self.journal.write(attempt_id, record)
        return result

    @staticmethod
    def _collect(backend, native, manifest):
        permitted = manifest["request"].get("outputs", [])
        collected = backend.collect(native, outputs=permitted)
        artifacts = {}
        total = 0
        for path, data in collected.get("artifacts", {}).items():
            if path not in permitted or not isinstance(data, bytes):
                raise ValueError("backend returned unauthorized artifact")
            total += len(data)
            if total > 10 * 1024 * 1024:
                raise ValueError("backend artifact limit exceeded")
            artifacts[path] = base64.b64encode(data).decode("ascii")
        def bounded_text(value):
            if not isinstance(value, str):
                raise ValueError("backend log is not text")
            return value.encode("utf-8", "replace")[:65536].decode("utf-8", "ignore")

        return {"stdout": bounded_text(collected.get("stdout", "")),
                "stderr": bounded_text(collected.get("stderr", "")),
                "artifacts": artifacts}


def main(*, once=False):
    """Entry point called by the installation CLI; secrets never appear in argv."""
    config = strict_json(Path(os.environ["BROKER_WORKER_CONFIG"]).read_bytes())
    client = JsonClient(
        os.environ.get("BROKER_CONTROL_URL", "http://127.0.0.1:8765"),
        environment_token("BROKER_WORKER_TOKEN"),
        allow_insecure_http=os.environ.get("BROKER_ALLOW_INSECURE_HTTP") == "1",
        max_response_bytes=384 * 1024 * 1024,
    )
    worker = Worker(client, config.get("connections", {}),
                    Journal(os.environ.get("BROKER_WORKER_JOURNAL", "/var/lib/compute-broker/worker")),
                    credential_profiles=config.get("credential_profiles", {}), extensions=config.get("extensions"))
    while True:
        try:
            worked = worker.poll_once()
        except Exception:
            logging.getLogger(__name__).error("Worker operation failed; preserving uncertain state")
            worked = False
        if once:
            return
        # The control service also schedules per-attempt observation backoff.
        # Never spin a healthy active job into an unbounded scheduler poll loop.
        time.sleep(1 if worked else 2)
