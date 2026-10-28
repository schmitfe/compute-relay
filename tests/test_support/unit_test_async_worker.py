import asyncio
import base64
import hashlib
from types import SimpleNamespace
from uuid import uuid4
import pytest
from compute_relay.backends import prepare
from compute_relay.worker.runner import Worker
from compute_relay.worker.client import ControlError
from compute_relay.worker.journal import Journal

class Control:
    def __init__(self):
        self.calls, self.fail_report, self.lease = [], False, None

    async def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        await asyncio.sleep(0)
        if path.endswith("/dispatch"):
            return {"authorized": True}
        if path.endswith("/report") and self.fail_report:
            self.fail_report = False
            raise ControlError("CONTROL_UNAVAILABLE")
        if path == "/internal/v1/lease":
            return {"lease": self.lease}
        return {}

class Backend:
    def __init__(self):
        self.submitted, self.reconciled, self.cancelled = 0, 0, 0
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.release.set()
        self.output_failure = False

    async def submit(self, attempt_id, manifest, files):
        self.submitted += 1
        self.entered.set()
        await self.release.wait()
        return {"outcome": "submitted", "native_identity": {"job_id": "17", "attempt_id": attempt_id}}

    async def reconcile(self, attempt_id, manifest):
        self.reconciled += 1
        return {"outcome": "submission_uncertain"}

    async def observe(self, items):
        return {item["native_identity"]["attempt_id"]: {"state": "completed", "terminal": True, "accounting": {"cpu_core_seconds": 2, "gpu_device_seconds": 0, "final": True}} for item in items}

    async def cancel(self, native, *, manifest):
        self.cancelled += 1
        return {"outcome": "cancel_requested"}

    async def collect(self, native, outputs, *, manifest):
        if self.output_failure:
            raise OSError("private remote diagnostic")
        return {"stdout": "synthetic result", "stderr": "", "artifacts": {}}

@pytest.fixture
def configured(tmp_path):
    control, backend = Control(), Backend()
    connection_id, owner_id = str(uuid4()), str(uuid4())
    connection = {"backend": "fake", "revision_sha256": "c" * 64}
    journal = Journal(tmp_path / "journal")
    worker = Worker(control, {connection_id: connection}, journal, owner_id=owner_id,
                         backend_factory=lambda _: backend, extensions=[])
    source = b"raise RuntimeError('must never execute in a connector')\n"
    request = {"schema_version": "1.0", "project_id": str(uuid4()), "connection_id": connection_id, "name": "async-test",
        "command": {"interpreter": "python3", "script": "main.py", "arguments": []},
        "environment": {"id": str(uuid4()), "revision_sha256": "b" * 64},
        "resources": {"nodes": 1, "cpus": 1, "gpus": 0, "memory_mib": 128, "time_limit_seconds": 60},
        "slurm": {"partition": "debug", "account": "broker"}, "outputs": [],
        "files": [{"path": "main.py", "sha256": hashlib.sha256(source).hexdigest(), "size_bytes": len(source), "executable": False}]}
    package = prepare(request, {**request["environment"], "interpreter": "/usr/bin/python3"}, connection, str(uuid4()))
    files = {"main.py": source, **{path: text.encode() for path, text in package["generated_files"].items()}}
    lease = {"id": str(uuid4()), "operation": "submit", "attempt_id": str(uuid4()), "generation": 1,
        "connection_id": connection_id, "native_identity": None, "prepared": {"manifest": package["manifest"],
        "manifest_sha256": package["manifest_sha256"], "files": {path: base64.b64encode(data).decode() for path, data in files.items()}}}
    return SimpleNamespace(worker=worker, lease=lease, control=control, backend=backend, journal=journal)
