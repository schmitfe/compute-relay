"""Slurm worker with native async SSH, bundled hooks and bounded owner lanes.

Local journal verification/fsync remains synchronous on a protected filesystem;
network I/O and optional extension helpers use awaitable HTTP/OpenSSH transports.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
import time
from uuid import UUID

from .async_client import AsyncJsonClient
from .client import ControlError, environment_token, strict_json
from .concurrency import PrincipalScheduler
from .enrollment import EnrollmentError, profile_metadata, resolve_connection
from .journal import Journal
from .runner import Worker
from .lease import renewing_async
from compute_relay.domain.lifecycle import (CancellationStatus, PreparedWork, cancellation_result, cancellation_status,
    native_for, replay_result, report_record, submission_plan, submission_record, terminal_observation)


class AsyncWorker(Worker):
    def __init__(self, *args, owner_id, scheduler=None, extensions=None, **kwargs):
        from compute_relay.extensions import async_worker_names, configured_names
        requested = configured_names(",".join(extensions)) if extensions is not None else configured_names()
        super().__init__(*args, extensions=async_worker_names(requested), **kwargs)
        self.scheduler = scheduler or PrincipalScheduler()
        self.owner_id = self.scheduler.register(owner_id)

    @staticmethod
    def _backend(connection):
        from compute_relay.backends import create_backend
        return create_backend(connection, asynchronous=True)

    async def poll_once(self):
        return await self.scheduler.run(self.owner_id, self._poll_once)

    async def _poll_once(self):
        await self.replay_pending_reports()
        if self._profiles_published_at is None or time.monotonic() - self._profiles_published_at >= 60:
            await self.publish_profiles()
        lease = (await self.client.request("POST", "/internal/v1/lease", {})).get("lease")
        if lease is None:
            return False
        await self._execute(lease)
        return True

    async def publish_profiles(self):
        profiles = []
        for alias, profile in sorted(self.credential_profiles.items()):
            try:
                profiles.append(profile_metadata(alias, profile))
            except EnrollmentError:
                logging.getLogger(__name__).warning("A credential profile is invalid and was not advertised")
        await self.client.request("POST", "/internal/v1/worker/profiles", {"profiles": profiles, "extensions": list(self.extensions)})
        self._profiles_published_at = time.monotonic()

    async def execute(self, lease):
        return await self.scheduler.run(self.owner_id, lambda: self._execute(lease))

    async def discover(self, lease):
        connection_id = str(UUID(lease["connection_id"]))
        report = {"lease_id": lease["id"], "generation": lease["generation"]}
        try:
            connection, public, revision = resolve_connection(lease["configuration"], self.credential_profiles, refresh=True)
            snapshot = await self.backend_factory(connection).discover()
            report.update(outcome="discovered", configuration=public, connection_revision_sha256=revision, discovery_snapshot=snapshot)
        except EnrollmentError as error:
            report.update(outcome="discovery_failed", error_code=error.code)
        except Exception:
            report.update(outcome="discovery_failed", error_code="DISCOVERY_UNAVAILABLE")
        await self.client.request("POST", f"/internal/v1/connections/{connection_id}/report", report)
        if report["outcome"] == "discovered":
            self.connections[connection_id] = connection

    async def _execute(self, lease):
        operation = lease.get("operation")
        if operation == "discover":
            await self.discover(lease)
            return
        if operation == "extension":
            from compute_relay.extensions import execute_extension_work_async
            await execute_extension_work_async(self, lease)
            return
        work = PreparedWork.decode(lease)
        operation, attempt_id = work.operation, work.attempt_id
        public = lease.get("configuration", {})
        connection = resolve_connection(public, self.credential_profiles)[0] if public.get("credential_profile") else self.connections[lease["connection_id"]]
        manifest, digest, files = work.manifest, work.manifest_sha256, work.files
        async with self.journal.async_locked(attempt_id):
            previous = self.journal.read(attempt_id)
            work.verify(connection, previous, self.extensions)
            native = native_for(lease, previous)
            saved = self.journal.read_report(attempt_id)
            result = replay_result(saved, lease, digest, native)
            if result is not None and saved["phase"] == "ready":
                await self.deliver_report(attempt_id, report_record(lease, digest, result))
                return
            backend = self.backend_factory(connection)
            async with renewing_async(self.client, lease):
                if operation in {"submit", "reconcile"}:
                    result = await self._submission(lease, backend, manifest, files, previous)
                elif not native:
                    raise ValueError("Management operation has no native identity")
                elif result is None:
                    if operation == "cancel":
                        try:
                            cancelled = await backend.cancel(native, manifest=manifest)
                        except Exception:
                            cancelled = {"outcome": "unknown"}
                        observation = None
                        if cancellation_status(cancelled) in {CancellationStatus.UNKNOWN, CancellationStatus.ALREADY_TERMINAL}:
                            try:
                                observation = await backend.observe(native, manifest=manifest)
                            except Exception:
                                pass
                        result = cancellation_result(cancelled, native, observation)
                    else:
                        result = {"outcome": "observed", "native_identity": native,
                            "observation": await backend.observe(native, manifest=manifest)}
                if result.get("outcome") == "observed" and terminal_observation(result.get("observation")):
                    self.journal.write_report(attempt_id, report_record(lease, digest, result, phase="observed"))
                    try:
                        from compute_relay.extensions import worker_after_observation_async
                        extension_data = await worker_after_observation_async(self.extensions, backend, native, manifest,
                            lease.get("extension_context", {}))
                        if extension_data:
                            result["extension_data"] = extension_data
                    except Exception:
                        result["extension_data"] = {manifest.get("extension_name", ""): {"error": "FINALIZATION_UNAVAILABLE"}}
                    try:
                        collected = await backend.collect(native, outputs=manifest["request"].get("outputs", []), manifest=manifest)
                        result.update(self._serialized_collection(collected, manifest))
                    except Exception:
                        result.update(artifacts={}, error_code="OUTPUT_COLLECTION_FAILED")
                record = report_record(lease, digest, result)
                self.journal.write_report(attempt_id, record)
            await self.deliver_report(attempt_id, record)

    async def deliver_report(self, attempt_id, record):
        self.journal.write_report(attempt_id, record)
        try:
            await self.client.request("POST", f"/internal/v1/attempts/{attempt_id}/report", record["report"])
        except ControlError as error:
            if error.status != 409:
                raise
            await self.client.request("POST", f"/internal/v1/attempts/{attempt_id}/renew",
                {key: record["report"][key] for key in ("lease_id", "generation")})
            await self.client.request("POST", f"/internal/v1/attempts/{attempt_id}/report", record["report"])
        self.journal.clear_report(attempt_id, record)

    async def replay_pending_reports(self):
        for attempt_id, _ in self.journal.pending_reports():
            async with self.journal.async_locked(attempt_id):
                record = self.journal.read_report(attempt_id)
                if not record or record.get("phase") != "ready":
                    continue
                try:
                    await self.deliver_report(attempt_id, record)
                except ControlError as error:
                    if error.status != 409:
                        raise

    async def _submission(self, lease, backend, manifest, files, previous):
        attempt_id = str(UUID(lease["attempt_id"]))
        digest = lease["prepared"]["manifest_sha256"]
        action, replayed = submission_plan(lease["operation"], previous)
        if action == "replay":
            return replayed
        if action == "reconcile":
            result = await backend.reconcile(attempt_id, manifest)
        else:
            confirmed = await self.client.request("POST", f"/internal/v1/attempts/{attempt_id}/dispatch", {
                "lease_id": lease["id"], "generation": lease["generation"], "manifest_sha256": digest,
                "connection_revision_sha256": manifest["connection_revision_sha256"]})
            if not confirmed.get("authorized"):
                raise ControlError("DISPATCH_DENIED")
            self.journal.write(attempt_id, {"manifest_sha256": digest, "outcome": "intent"})
            try:
                result = await backend.submit(attempt_id, manifest, files)
            except Exception:
                result = {"outcome": "submission_uncertain"}
        record = submission_record(digest, result)
        self.journal.write(attempt_id, record)
        return record["result"]


async def serve_workers(workers, *, once=False):
    """A fixed owner registry; each owner polls independently within shared bounds."""
    if not workers or len(workers) > 64 or len({id(worker.scheduler) for worker in workers}) != 1:
        raise ValueError("Configure one through 64 workers sharing one principal scheduler")

    async def serve(worker):
        while True:
            try:
                worked = await worker.poll_once()
            except Exception:
                logging.getLogger(__name__).error("Async worker operation failed; preserving uncertain state")
                worked = False
            if once:
                return
            await asyncio.sleep(1 if worked else 2)

    async with asyncio.TaskGroup() as group:
        for worker in workers:
            group.create_task(serve(worker))


async def main(*, once=False):
    config = strict_json(Path(os.environ["BROKER_WORKER_CONFIG"]).read_bytes())
    owner_id = str(UUID(os.environ.get("BROKER_WORKER_OWNER_ID", config.get("owner_id", ""))))
    scheduler = PrincipalScheduler(max_concurrency=config.get("max_concurrency", 4), max_pending=config.get("max_pending", 16))
    client = AsyncJsonClient(os.environ.get("BROKER_CONTROL_URL", "http://127.0.0.1:8765"), environment_token("BROKER_WORKER_TOKEN"),
        allow_insecure_http=os.environ.get("BROKER_ALLOW_INSECURE_HTTP") == "1", max_response_bytes=384 * 1024 * 1024)
    try:
        worker = AsyncWorker(client, config.get("connections", {}), Journal(os.environ.get("BROKER_WORKER_JOURNAL", "/var/lib/compute-relay/worker")),
            credential_profiles=config.get("credential_profiles", {}), extensions=config.get("extensions"), owner_id=owner_id, scheduler=scheduler)
        await serve_workers([worker], once=once)
    finally:
        await client.close()
