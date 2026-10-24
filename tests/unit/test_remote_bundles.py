"""Fixed source packaging and cross-upgrade management without a cluster."""

import ast
import asyncio
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import pytest

from compute_relay.backends import (
    BackendError, SlurmBackend, canonical_json_bytes, management_protocol_supported,
    prepare, submission_helper_is_current, verify_package,
)
from compute_relay.backends.preparation import helper_source
from compute_relay.backends.async_slurm import AsyncSlurmBackend
from compute_relay.remote import bundles


def digest(value):
    return hashlib.sha256(value).hexdigest()


def test_fixed_bundles_are_deterministic_stdlib_programs_with_complete_sources():
    expected = {
        "jobs": {"filesystem.py", "scheduler.py", "protocol.py", "jobs.py", "collection.py", "job_dispatch.py", "entrypoint.py"},
        "discovery": {"filesystem.py", "scheduler.py", "discovery_config.py", "module_catalog.py", "software.py", "discovery.py", "entrypoint.py"},
    }
    for kind, names in expected.items():
        source = bundles.bundle_source(kind)
        assert source == bundles.bundle_source(kind)
        assert digest(source.encode()) == digest(bundles.bundle_source(kind).encode())
        assert set(bundles.component_names(kind)) == names
        assert all(source.count("# Component: " + name + "\n") == 1 for name in names)
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                assert all(item.name in sys.stdlib_module_names for item in node.names)
            if isinstance(node, ast.ImportFrom):
                assert node.level == 0 and node.module.split(".")[0] in sys.stdlib_module_names
        compile(source, "<fixed-bundle>", "exec")
    assert "software_discovery" not in helper_source()
    assert "def submit(" not in helper_source("discover")
    with pytest.raises(ValueError):
        bundles.bundle_source("../archived")
    with pytest.raises(BackendError):
        helper_source("shell")


def test_discovery_only_change_does_not_change_execution_identity(monkeypatch):
    execution = helper_source()
    discovery = helper_source("discover")
    original = bundles._component
    monkeypatch.setattr(bundles, "_component", lambda name: original(name) + ("# catalog edit\n" if name == "module_catalog.py" else ""))
    assert helper_source() == execution
    assert helper_source("discover") != discovery


class LocalFixedTransport:
    """Only installed first-party helper source runs; payload files stay inert."""

    def __init__(self):
        self.operations = []

    def call_helper(self, source, request):
        self.operations.append(request["operation"])
        result = subprocess.run([sys.executable, "-I", "-c", source],
                                input=json.dumps(request).encode(), capture_output=True, timeout=10, check=False)
        if result.returncode:
            raise BackendError("Fixed fixture helper rejected request")
        return json.loads(result.stdout)


class AsyncLocalFixedTransport(LocalFixedTransport):
    async def call_helper(self, source, request):
        self.operations.append(request["operation"])
        process = await asyncio.create_subprocess_exec(sys.executable, "-I", "-c", source,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, _ = await asyncio.wait_for(process.communicate(json.dumps(request).encode()), timeout=10)
        if process.returncode:
            raise BackendError("Fixed fixture helper rejected request")
        return json.loads(stdout)


@pytest.fixture
def site(tmp_path):
    state = tmp_path / "scheduler.json"
    calls = tmp_path / "calls"
    program = tmp_path / "scheduler"
    # A fixed scheduler fixture records accepted jobs but never starts uploaded
    # scripts. Its queue/accounting identity is generated from submitted marker.
    program.write_text(f'''#!{sys.executable}
import datetime,json,sys
from pathlib import Path
state=Path({str(state)!r}); calls=Path({str(calls)!r})
command=Path(sys.argv[0]).name
with calls.open('a') as stream: stream.write(command+'\\n')
data=json.loads(state.read_text()) if state.exists() else {{}}
if command=='sbatch':
    data.update(marker=next(item.split('=',1)[1] for item in sys.argv[1:] if item.startswith('--comment=')),
        submitted=datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S'),state='RUNNING')
    state.write_text(json.dumps(data))
    if data.get('lose_reply'): sys.exit(1)
    print('42;fixture')
elif command=='squeue':
    if data.get('state')=='RUNNING':
        fmt=sys.argv[sys.argv.index('-o')+1]
        print(('42|RUNNING|' if '%T' in fmt else '42|')+data['marker']+'|'+data['submitted'])
elif command=='scancel':
    data['state']='CANCELLED'; state.write_text(json.dumps(data))
elif command=='sacct':
    if data.get('state')=='CANCELLED':
        print('42|CANCELLED|1|1|cpu=1|0:0|'+data['submitted']+'|'+data['submitted']+'|'+data['submitted']+'|'+data['marker']+'|'+data.get('work_dir','')+'|')
''')
    program.chmod(0o700)
    paths = {}
    for name in ("sbatch", "squeue", "sacct", "scancel"):
        path = tmp_path / name
        path.symlink_to(program)
        paths[name] = str(path)
    connection = {"backend": "slurm", "revision_sha256": "f" * 64, "remote_root": str(tmp_path / "jobs"), "slurm_paths": paths,
                  "site_profile": {"partitions": ["debug"]}}
    script = b"raise RuntimeError('submitted code must stay inert in this fixture')\n"
    request = {"schema_version": "1.0", "project_id": str(uuid4()), "connection_id": str(uuid4()), "name": "upgrade-test",
               "command": {"interpreter": "python3", "script": "main.py", "arguments": []},
               "environment": {"id": str(uuid4()), "revision_sha256": "a" * 64},
               "resources": {"nodes": 1, "cpus": 1, "gpus": 0, "memory_mib": 128, "time_limit_seconds": 60},
               "slurm": {"partition": "debug"}, "outputs": ["result.txt"],
               "files": [{"path": "main.py", "sha256": digest(script), "size_bytes": len(script), "executable": False}]}
    return connection, request, script, state, calls


def prepared(site):
    connection, request, script, *_ = site
    value = prepare(request, {**request["environment"], "interpreter": "/usr/bin/python3"}, connection, str(uuid4()))
    files = {"main.py": script, **{path: text.encode() for path, text in value["generated_files"].items()}}
    return value, files


@pytest.mark.parametrize("legacy_protocol", [False, True])
def test_submitted_job_remains_manageable_across_execution_bundle_upgrade(site, monkeypatch, legacy_protocol):
    original = bundles._component
    transport = LocalFixedTransport()
    backend = SlurmBackend(site[0], transport=transport)
    attempt = str(uuid4())
    # Version N has a reviewed comment-only source difference. No source is
    # retrieved from a manifest/journal and no archive is executed on recovery.
    with monkeypatch.context() as old:
        old.setattr(bundles, "_component", lambda name: original(name) + ("# version N\n" if name == "jobs.py" else ""))
        package, files = prepared(site)
        if legacy_protocol:
            package["manifest"].pop("helper_protocol")
            package["manifest_sha256"] = digest(canonical_json_bytes(package["manifest"]))
        native = backend.submit(attempt, package["manifest"], files)["native_identity"]
    manifest = package["manifest"]
    assert management_protocol_supported(manifest)
    assert not submission_helper_is_current(manifest)
    with pytest.raises(BackendError, match="helper revision"):
        verify_package(manifest, package["manifest_sha256"], files)
    for operation in ("observe", "cancel", "collect", "reconcile"):
        verify_package(manifest, package["manifest_sha256"], files, operation=operation)
    assert backend.reconcile(attempt, manifest)["native_identity"] == native
    assert backend.observe(native, manifest=manifest)["state"] == "running"
    assert backend.cancel(native, manifest=manifest)["outcome"] == "cancel_requested"
    observation = backend.observe(native, manifest=manifest)
    assert observation["terminal"] is True
    assert observation["accounting"] == {"final": True, "cpu_core_seconds": 1, "gpu_device_seconds": 0}
    directory = Path(native["work_dir"])
    (directory / "result.txt").write_text("bounded artifact")
    assert backend.collect(native, ["result.txt"], manifest=manifest)["artifacts"] == {"result.txt": b"bounded artifact"}
    assert site[4].read_text().splitlines().count("sbatch") == 1


def test_uncertain_old_submission_reconciles_without_resubmission_after_upgrade(site, monkeypatch):
    site[3].write_text(json.dumps({"lose_reply": True}))
    original = bundles._component
    transport = LocalFixedTransport()
    backend = SlurmBackend(site[0], transport=transport)
    attempt = str(uuid4())
    with monkeypatch.context() as old:
        old.setattr(bundles, "_component", lambda name: original(name) + ("# previous helper\n" if name == "jobs.py" else ""))
        package, files = prepared(site)
        assert backend.submit(attempt, package["manifest"], files)["outcome"] == "submission_uncertain"
    assert backend.reconcile(attempt, package["manifest"])["outcome"] == "submitted"
    assert backend.reconcile(attempt, package["manifest"])["outcome"] == "submitted"
    assert transport.operations == ["submit", "reconcile", "reconcile"]
    assert site[4].read_text().splitlines().count("sbatch") == 1


def test_native_async_management_uses_current_bundle_after_upgrade_and_uncertain_reply(site, monkeypatch):
    async def scenario():
        original = bundles._component
        transport = AsyncLocalFixedTransport()
        backend = AsyncSlurmBackend(site[0], transport=transport)
        site[3].write_text(json.dumps({"lose_reply": True}))
        attempt = str(uuid4())
        with monkeypatch.context() as old:
            old.setattr(bundles, "_component", lambda name: original(name) + ("# old async bundle\n" if name == "jobs.py" else ""))
            package, files = prepared(site)
            assert (await backend.submit(attempt, package["manifest"], files))["outcome"] == "submission_uncertain"
        manifest = package["manifest"]
        native = (await backend.reconcile(attempt, manifest))["native_identity"]
        assert (await backend.observe(native, manifest=manifest))["state"] == "running"
        assert (await backend.cancel(native, manifest=manifest))["outcome"] == "cancel_requested"
        assert (await backend.observe(native, manifest=manifest))["accounting"]["final"] is True
        assert (await backend.collect(native, manifest=manifest))["artifacts"] == {}
        assert (await backend.reconcile(attempt, manifest))["native_identity"] == native
        assert site[4].read_text().splitlines().count("sbatch") == 1
    asyncio.run(scenario())


@pytest.mark.parametrize("outcome", ["unknown", "identity_mismatch", "cancel_requested", "already_terminal", "bogus", None])
def test_backend_cancel_reports_only_explicit_understood_results(site, outcome):
    class Transport:
        def call_helper(self, source, request):
            return {"outcome": outcome, "detail": "private diagnostic must not leak"} if outcome is not None else None
    package, _ = prepared(site)
    actual = SlurmBackend(site[0], transport=Transport()).cancel({}, manifest=package["manifest"])
    assert actual == {"outcome": outcome if outcome in {"unknown", "identity_mismatch", "cancel_requested", "already_terminal"} else "unknown"}


@pytest.mark.parametrize("operation", ["submit", "observe", "cancel", "collect", "reconcile"])
def test_upgrade_management_never_weakens_package_integrity(site, operation):
    package, files = prepared(site)
    changed = {**files, "main.py": files["main.py"] + b"# tamper"}
    with pytest.raises(BackendError, match="does not match"):
        verify_package(package["manifest"], package["manifest_sha256"], changed, operation=operation)
    manifest = copy.deepcopy(package["manifest"])
    manifest["request"]["name"] = "changed"
    with pytest.raises(BackendError, match="digest mismatch"):
        verify_package(manifest, package["manifest_sha256"], files, operation=operation)


@pytest.mark.parametrize("change", ["digest", "embedded_manifest", "native", "protocol"])
def test_current_management_requires_exact_original_journal_and_native_binding(site, change):
    transport = LocalFixedTransport()
    backend = SlurmBackend(site[0], transport=transport)
    package, files = prepared(site)
    native = backend.submit(str(uuid4()), package["manifest"], files)["native_identity"]
    before = site[4].read_text()
    manifest = copy.deepcopy(package["manifest"])
    if change == "digest":
        manifest["request"]["name"] = "unapproved"
    elif change == "embedded_manifest":
        path = Path(site[0]["remote_root"]) / "_broker_journal" / (native["attempt_id"] + ".json")
        record = json.loads(path.read_text())
        record["manifest"]["request"]["name"] = "mutated journal"
        path.write_text(json.dumps(record))
    elif change == "native":
        native = {**native, "job_id": "43"}
    else:
        manifest["helper_protocol"] = "slurm-jobs/v999"
    with pytest.raises(BackendError):
        backend.cancel(native, manifest=manifest)
    assert site[4].read_text() == before
