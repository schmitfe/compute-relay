"""Current scheduler observations must not masquerade as admission authority."""

from copy import deepcopy
from datetime import timedelta
import asyncio
import json
import io
import sys
from pathlib import Path
from uuid import uuid4

from django.test import Client
from django.utils import timezone
import pytest

from compute_relay.backends.availability import parse_availability
from compute_relay.control import availability
from compute_relay.backends import SlurmBackend
from compute_relay.backends.async_slurm import AsyncSlurmBackend
from compute_relay.worker.runner import Worker
from compute_relay.worker.async_runner import AsyncWorker
from compute_relay.worker.journal import Journal
from compute_relay.worker.client import JsonClient
from test_support.unit_test_control import broker, budgeted, dispatched, prepared_job
from test_support.unit_test_remote_bundles import LocalFixedTransport, AsyncLocalFixedTransport


RAW = {"nodes": {"returncode": 0, "stdout": (
    "n1|gpu|mixed|32|gpu:h200:4(S:0-1)|gpu:h200:3(IDX:0-2)\n"
    "n2|gpu|down|32|gpu:h200:4|gpu:h200:0\n"
    "n3|visual|idle|16|gpu:a6000:2|gpu:a6000:0\n")},
    "cpus": {"returncode": 0, "stdout": "n1|gpu|16/16/0/32\nn2|gpu|0/32/0/32\nn3|visual|0/16/0/16\n"},
    "queue": {"returncode": 0, "stdout": (
        "gpu|PENDING||gres/gpu:h200:1|||Resources\n"
        "gpu|PENDING|gres/gpu:h200:1||||Priority\n"
        "gpu|RUNNING||gres/gpu:h200:1|||None\n"
        "visual|PENDING||||gres/gpu:a6000:1|QOSGrpGRES\n")}}


def test_snapshot_groups_hardware_and_queue_pressure_and_excludes_down_capacity():
    parsed = parse_availability(RAW)
    gpu, visual = parsed["partitions"]
    assert gpu["gpus"]["h200"] == {"configured": 8, "schedulable": 4, "allocated": 3, "idle": 1, "allocation_known": True}
    assert gpu["cpus"]["idle"] == 16 and gpu["schedulable_nodes"] == 1
    assert gpu["queue"]["pending_by_gpu_type"] == {"h200": 2}
    assert gpu["queue"]["pending_reasons"] == {"Resources": 1, "Priority": 1}
    assert gpu["queue_pressure"]["pending_jobs_per_schedulable_node"] == "2"
    assert gpu["queue_pressure"]["pending_gpu_jobs_per_schedulable_device"] == {"h200": "0.5"}
    assert visual["gpus"]["a6000"]["idle"] == 2
    assert parsed["placement_guaranteed"] is False and parsed["queue_wait_seconds"] is None


@pytest.mark.parametrize("used", ["gpu:3", "(null)", "N/A", "", "gpu:h200:9"])
def test_unknown_gpu_allocation_is_not_reported_as_free(used):
    raw = deepcopy(RAW)
    raw["nodes"]["stdout"] = f"n1|gpu|mixed|32|gpu:h200:4|{used}\n"
    parsed = parse_availability(raw)["partitions"][0]
    assert parsed["gpus"]["h200"]["idle"] is None
    assert parse_availability({})["nodes_status"] == "unknown"


def test_failed_queries_and_unknown_cpu_data_never_infer_free_capacity():
    raw = deepcopy(RAW)
    raw["nodes"]["returncode"] = 1
    parsed = parse_availability(raw)
    assert all(not row["gpus"] for row in parsed["partitions"])
    assert all(row["queue_pressure"]["pending_jobs_per_schedulable_node"] is None for row in parsed["partitions"])
    raw = deepcopy(RAW)
    raw["cpus"]["returncode"] = 1
    assert parse_availability(raw)["partitions"][0]["cpus"]["idle"] is None
    raw["nodes"]["stdout"] = "n1|gpu|mixed|32|gpu:h200:4,shard:40|gpu:h200:0,shard:3\n"
    assert parse_availability(raw)["partitions"][0]["gpus"]["h200"]["idle"] is None


def test_pressure_compares_cluster_size_and_gpu_type_instead_of_absolute_queue_length():
    def snapshot(nodes, pending):
        return parse_availability({"nodes": {"returncode": 0, "stdout": "".join(
            f"n{index}|gpu|allocated|32|gpu:h200:4|gpu:h200:4\n" for index in range(nodes))},
            "queue": {"returncode": 0, "stdout": "gpu|PENDING||gres/gpu:h200:1|||Resources\n" * pending}})["partitions"][0]
    small, large = snapshot(5, 10), snapshot(200, 100)
    assert small["queue"]["pending_jobs"] < large["queue"]["pending_jobs"]
    assert small["queue_pressure"]["pending_jobs_per_schedulable_node"] == "2"
    assert large["queue_pressure"]["pending_jobs_per_schedulable_node"] == "0.5"
    assert small["queue_pressure"]["pending_gpu_jobs_per_schedulable_device"]["h200"] == "0.5"
    assert large["queue_pressure"]["pending_gpu_jobs_per_schedulable_device"]["h200"] == "0.125"
    missing_type = deepcopy(RAW)
    missing_type["queue"]["stdout"] += "gpu|PENDING||gres/gpu:a6000:1|||Resources\n"
    assert parse_availability(missing_type)["partitions"][0]["queue_pressure"]["pending_gpu_jobs_per_schedulable_device"]["a6000"] is None


@pytest.mark.parametrize("failure", ["no_nodes", "truncated", "failed_queue", "untyped"])
def test_pressure_exposes_unknown_capacity_and_untyped_requests(failure):
    raw = deepcopy(RAW)
    if failure == "no_nodes":
        raw["nodes"]["stdout"] = ""
    elif failure == "truncated":
        raw["nodes"]["truncated"] = True
    elif failure == "failed_queue":
        raw["queue"]["returncode"] = 1
    else:
        raw["queue"]["stdout"] += "gpu|PENDING||gres/gpu:1|||Resources\n"
    pressure = parse_availability(raw)["partitions"][0]["queue_pressure"]
    if failure == "untyped":
        assert pressure["untyped_pending_gpu_jobs"] == 1
        assert pressure["pending_gpu_jobs_per_schedulable_device"]["unspecified"] is None
    else:
        assert pressure["status"] == "unknown" and pressure["pending_jobs_per_schedulable_node"] is None


def test_refresh_is_key_scoped_rate_limited_fenced_and_does_not_change_revision(broker):
    c = broker
    c.worker.execution_features = ["availability_v1"];c.worker.save(update_fields=["execution_features"])
    c.connection.backend = "slurm"
    c.connection.configuration = {**c.connection.configuration, "site_profile": {"partitions": ["gpu"]}}
    c.connection.save()
    client = Client(HTTP_AUTHORIZATION="Bearer " + c.token)
    endpoint = f"/api/v1/projects/{c.project.id}/availability"
    requested = client.post(endpoint, "{}", content_type="application/json")
    assert requested.json()["connections"][0]["availability"]["refresh_pending"]
    lease = availability.lease_availability(c.worker)["lease"]
    assert availability.lease_availability(c.worker) is None
    c.connection.refresh_from_db()
    revision = c.connection.revision_sha256
    report = {"lease_id": lease["id"], "generation": lease["generation"], "connection_revision_sha256": revision,
        "snapshot": parse_availability(RAW)}
    worker = Client(HTTP_AUTHORIZATION="Bearer synthetic-worker-token")
    path = f"/internal/v1/connections/{c.connection.id}/availability-report"
    assert worker.post(path, json.dumps({**report, "generation": lease["generation"] + 1}), content_type="application/json").status_code == 409
    assert worker.post(path, json.dumps(report), content_type="application/json").status_code == 200
    response = client.get(endpoint).json()["connections"][0]["availability"]
    assert response["status"] == "ready" and not response["stale"]
    assert [row["name"] for row in response["partitions"]] == ["gpu"]
    assert response["admission_limits_are_capacity"] is False
    assert client.post(endpoint, "{}", content_type="application/json").json()["connections"][0]["availability"]["refresh_pending"] is False
    c.connection.refresh_from_db()
    assert c.connection.revision_sha256 == revision
    c.credential.allowed_connection_ids = [];c.credential.save(update_fields=["allowed_connection_ids"])
    assert client.get(endpoint).json()["connections"] == []
    assert client.post(endpoint, json.dumps({"connection_ids": [str(c.connection.id)]}), content_type="application/json").status_code == 403
    assert not c.project.job_set.exists()


def test_stale_snapshot_and_old_connector_are_explicit(broker):
    c = broker
    c.connection.availability_snapshot = {**parse_availability(RAW), "connection_revision_sha256": c.connection.revision_sha256,
        "observed_at": (timezone.now() - timedelta(minutes=10)).isoformat()}
    c.connection.save()
    value = availability.public_availability(c.connection)
    assert value["stale"] and value["age_seconds"] >= 600 and value["connector_upgrade_required"]


def test_refresh_does_not_delay_uncertain_submission_recovery(broker):
    c = broker
    c.worker.execution_features = ["availability_v1"];c.worker.save()
    c.connection.availability_requested_at = timezone.now();c.connection.save()
    budgeted(c)
    attempt, lease = dispatched(c, prepared_job(c))
    # The expired dispatch lease must reconcile before a scheduler snapshot.
    attempt.refresh_from_db()
    attempt.lease_expires_at = timezone.now() - timedelta(seconds=1)
    attempt.save(update_fields=["lease_expires_at"])
    assert availability.lease_availability(c.worker) is None


def test_connector_leaves_job_processing_time_between_cluster_refreshes(broker):
    c = broker
    c.worker.execution_features = ["availability_v1"];c.worker.save()
    c.connection.availability_requested_at = timezone.now();c.connection.save()
    other = deepcopy(c.connection)
    other.id = uuid4();other.save()
    first = availability.lease_availability(c.worker)["lease"]
    assert availability.lease_availability(c.worker) is None
    from compute_relay.control.models import Connection
    Connection.objects.filter(id=first["connection_id"]).update(availability_requested_at=None,
        availability_lease_expires_at=None, availability_checked_at=timezone.now())
    assert availability.lease_availability(c.worker) is None
    Connection.objects.filter(id=first["connection_id"]).update(availability_checked_at=timezone.now() - timedelta(seconds=6))
    assert availability.lease_availability(c.worker)["lease"]["connection_id"] != first["connection_id"]


@pytest.mark.parametrize("asynchronous", [False, True])
def test_fixed_scheduler_helper_and_worker_report_both_execution_modes(tmp_path, asynchronous):
    program = tmp_path / "scheduler"
    calls = tmp_path / "calls.jsonl"
    program.write_text(f'''#!{sys.executable}
import json,sys
from pathlib import Path
with Path({str(calls)!r}).open('a') as stream: stream.write(json.dumps(sys.argv)+'\\n')
raw={RAW!r}
name='queue' if Path(sys.argv[0]).name=='squeue' else 'cpus' if 'CPUsState' in sys.argv[-1] else 'nodes'
sys.stdout.write(raw[name]['stdout'])
''')
    program.chmod(0o700)
    paths = {}
    for name in ("sinfo", "squeue"):
        path = tmp_path / name
        path.symlink_to(program)
        paths[name] = str(path)
    identifier = str(uuid4())
    connection = {"backend": "slurm", "revision_sha256": "c" * 64, "remote_root": str(tmp_path / "remote"), "slurm_paths": paths}
    lease = {"operation": "availability", "id": str(uuid4()), "connection_id": identifier,
        "generation": 7, "configuration": {}, "connection_revision_sha256": connection["revision_sha256"]}
    reports = []
    if asynchronous:
        class Control:
            async def request(self, method, path, body):
                reports.append((path, body))
        backend = AsyncSlurmBackend(connection, AsyncLocalFixedTransport())
        worker = AsyncWorker(Control(), {identifier: connection}, Journal(tmp_path / "journal"), owner_id=str(uuid4()),
            backend_factory=lambda _: backend, extensions=[])
        asyncio.run(worker.execute(lease))
    else:
        class Control:
            def request(self, method, path, body):
                reports.append((path, body))
        backend = SlurmBackend(connection, LocalFixedTransport())
        worker = Worker(Control(), {identifier: connection}, Journal(tmp_path / "journal"), backend_factory=lambda _: backend, extensions=[])
        worker.execute(lease)
    assert len(reports) == 1 and reports[0][0].endswith("/availability-report")
    report = reports[0][1]
    assert report["generation"] == 7 and report["lease_id"] == lease["id"]
    assert report["snapshot"]["partitions"][0]["gpus"]["h200"]["idle"] == 1
    commands = [json.loads(line) for line in calls.read_text().splitlines()]
    assert [Path(command[0]).name for command in commands] == ["sinfo", "sinfo", "squeue"]
    assert all(command[1] in {"-N", "-h"} and "-O" in command for command in commands)


@pytest.mark.parametrize("cached_numbers", [False, True])
def test_public_snapshots_round_trip_through_official_strict_client(broker, cached_numbers):
    c = broker
    snapshot = {**parse_availability(RAW), "connection_revision_sha256": c.connection.revision_sha256}
    if cached_numbers:
        # Existing observations and still-running connectors can contain the
        # numeric ratios which caused INVALID_CONTROL_RESPONSE in production.
        for row in snapshot["partitions"]:
            pressure = row["queue_pressure"]
            pressure.pop("ratio_encoding")
            pressure["pending_jobs_per_schedulable_node"] = float(pressure["pending_jobs_per_schedulable_node"])
            pressure["pending_gpu_jobs_per_schedulable_device"] = {
                key: float(value) for key, value in pressure["pending_gpu_jobs_per_schedulable_device"].items()}
    c.connection.availability_snapshot = snapshot
    c.connection.save()
    http = Client(HTTP_AUTHORIZATION="Bearer " + c.token)
    client = JsonClient("http://127.0.0.1:8768", c.token)
    class Opener:
        def open(self, request, **kwargs):
            response = http.generic(request.method, request.selector, data=request.data or b"", content_type="application/json")
            assert response.status_code == 200, response.content
            stream = io.BytesIO(response.content)
            stream.headers = response.headers
            return stream
    client._opener = Opener()
    for method in ("GET", "POST"):
        result = client.request(method, f"/api/v1/projects/{c.project.id}/availability", {} if method == "POST" else None)
        pressure = result["connections"][0]["availability"]["partitions"][0]["queue_pressure"]
        assert pressure["ratio_encoding"] == "decimal_string"
        assert pressure["pending_jobs_per_schedulable_node"] == "2"
        assert pressure["pending_gpu_jobs_per_schedulable_device"] == {"h200": "0.5"}
    client.request("GET", "/api/v1/context")
    client.request("GET", "/api/v1/connections")
    c.connection.refresh_from_db()
    assert c.connection.availability_snapshot == snapshot  # Reading does not rewrite cached evidence.


@pytest.mark.parametrize("partitions", [[None, "bad", 7], None, "bad"])
def test_old_malformed_availability_remains_readable(broker, partitions):
    from compute_relay.control.availability import public_availability
    broker.connection.availability_snapshot = {"partitions": partitions, "observed_at": 17}
    assert public_availability(broker.connection)["partitions"] == []
