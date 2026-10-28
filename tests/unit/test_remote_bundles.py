"""Fixed source packaging and cross-upgrade management without a cluster."""
from test_support.unit_test_remote_bundles import AsyncLocalFixedTransport, LocalFixedTransport, prepared, site

import ast
import asyncio
import copy
import hashlib
import importlib
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
        "jobs": {"prelude.py", "filesystem.py", "scheduler.py", "protocol.py", "jobs.py", "collection.py", "job_dispatch.py", "entrypoint.py"},
        "discovery": {"prelude.py", "filesystem.py", "scheduler.py", "discovery_config.py", "probes.py", "module_catalog.py", "software.py", "discovery.py", "entrypoint.py"},
    }
    expected["batch_status"] = expected["jobs"] | {"batch_status.py"}
    expected["stream_jobs"] = expected["batch_status"] | {"stream_entrypoint.py"}
    expected["availability"] = {"prelude.py", "filesystem.py", "scheduler.py", "availability.py", "entrypoint.py"}
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


def test_components_import_independently():
    for name in sorted(bundles._ALLOWED_COMPONENTS):
        importlib.import_module("compute_relay.remote." + name[:-3])


def test_a_borrowed_standard_library_import_fails_during_assembly(monkeypatch):
    original = bundles._component
    monkeypatch.setattr(bundles, "_component", lambda name: (
        original(name).replace("import re\n", "") if name == "scheduler.py" else original(name)))
    with pytest.raises(ValueError, match="Unbound component dependency in scheduler.py"):
        bundles.bundle_source("jobs")


@pytest.mark.parametrize("kind", ["jobs", "discovery"])
def test_component_declaration_order_does_not_change_bundles(kind, monkeypatch):
    expected = bundles.bundle_source(kind)
    original = bundles.component_names
    monkeypatch.setattr(bundles, "component_names", lambda selected: tuple(reversed(original(selected))))
    assert bundles.bundle_source(kind) == expected


@pytest.mark.parametrize("kind,missing", [("jobs", "filesystem.py"), ("discovery", "probes.py")])
def test_missing_components_fail_during_assembly(kind, missing, monkeypatch):
    original = bundles.component_names
    monkeypatch.setattr(bundles, "component_names", lambda selected: tuple(
        name for name in original(selected) if name != missing))
    with pytest.raises(ValueError, match="Missing component dependency"):
        bundles.bundle_source(kind)


@pytest.mark.parametrize("declaration", [
    "from .jobs import nonexistent", "from .unknown import submit",
    "from .jobs import *", "from .jobs import submit as renamed",
    "def hidden_import():\n    from .jobs import submit",
    "from .jobs import submit; another_statement = 1",
    "another_statement = 1; from .jobs import submit",
])
def test_unresolved_or_unsupported_component_imports_fail_closed(declaration, monkeypatch):
    original = bundles._component
    monkeypatch.setattr(bundles, "_component", lambda name: (
        original(name) + "\n" + declaration + "\n" if name == "entrypoint.py" else original(name)))
    with pytest.raises(ValueError, match="component (?:dependency|import)"):
        bundles.bundle_source("jobs")


def test_component_cycles_fail_during_assembly(monkeypatch):
    original = bundles._component
    monkeypatch.setattr(bundles, "_component", lambda name: (
        original(name) + "\nfrom .jobs import submit\n" if name == "scheduler.py" else original(name)))
    with pytest.raises(ValueError, match="contain a cycle"):
        bundles.bundle_source("jobs")


def test_discovery_only_change_does_not_change_execution_identity(monkeypatch):
    execution = helper_source()
    discovery = helper_source("discover")
    original = bundles._component
    monkeypatch.setattr(bundles, "_component", lambda name: original(name) + ("# catalog edit\n" if name == "module_catalog.py" else ""))
    assert helper_source() == execution
    assert helper_source("discover") != discovery










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


def test_helper_bundle_is_streamed_before_request(tmp_path):
    import json
    import shlex
    import subprocess
    import sys
    from compute_relay.transports import OpenSSHTransport
    transport = OpenSSHTransport({"python_path": sys.executable, "ssh": {
        "host": "cluster.example", "user": "owner", "known_hosts": str(tmp_path / "known_hosts")}})
    source = 'import sys,json\n# ' + "x" * 150000 + '\nprint(json.dumps({"request":json.load(sys.stdin),"unicode":"ä"}))\n'
    argv, payload = transport._helper_command(source, {"input": "\\quoted"})
    assert len(argv[-1]) < 512 and "\n" not in argv[-1] and "\\" not in argv[-1]
    result = subprocess.run(shlex.split(argv[-1]), input=payload, capture_output=True, check=True)
    assert json.loads(result.stdout) == {"request": {"input": "\\quoted"}, "unicode": "ä"}
