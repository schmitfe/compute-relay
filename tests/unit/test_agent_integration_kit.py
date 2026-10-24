"""Acceptance-kit tests execute only deterministic tiny local science."""

import copy
import importlib
import json
import math
from pathlib import Path
import stat
import subprocess
import sys
import tomllib
from uuid import uuid4

import pytest
from jsonschema import Draft202012Validator

import agent_integration as kit


ROOT = Path(__file__).resolve().parents[2]
COMMON = ROOT / "examples/agent-integration/common"


@pytest.fixture
def science(monkeypatch):
    monkeypatch.syspath_prepend(str(COMMON))
    return importlib.import_module("science"), importlib.import_module("dense_ising"), importlib.import_module("resources")


def generated(tmp_path, case="00"):
    return kit.create(case, tmp_path / "fresh-project", "http://127.0.0.1:8768",
                      project_token_file=str(tmp_path / "project-token-DO-NOT-READ"), python=sys.executable)


def test_generator_references_only_project_token_path_without_reading_it(tmp_path, monkeypatch):
    token = tmp_path / "project-token-DO-NOT-READ"
    token.write_text("SYNTHETIC_TOKEN_BYTES_MUST_NOT_BE_COPIED")
    original = Path.open

    def protected_open(path, *args, **kwargs):
        assert path != token, "Generator must never open token contents"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", protected_open)
    destination = generated(tmp_path)
    codex = tomllib.loads((destination / ".codex/config.toml").read_text())["mcp_servers"]["compute_relay"]
    claude = json.loads((destination / ".mcp.json").read_text())["mcpServers"]["compute_relay"]
    assert codex["command"] == sys.executable  # Preserve the venv executable, do not resolve its symlink.
    assert codex["args"] == ["-m", "compute_relay.mcp"]
    assert claude["type"] == "stdio" and claude["env"] == codex["env"]
    assert codex["env"]["BROKER_PROJECT_TOKEN_FILE"] == str(token)
    assert "BROKER_PROJECT_ID" not in codex["env"]
    assert json.loads((destination / "project.json").read_text())["project_identity_source"] == "api_key"
    for path in destination.rglob("*"):
        assert stat.S_IMODE(path.stat().st_mode) == (0o700 if path.is_dir() else 0o600)
        if path.is_file():
            assert b"SYNTHETIC_TOKEN_BYTES_MUST_NOT_BE_COPIED" not in path.read_bytes()
    assert not {"BROKER_WORKER_CONFIG", "BROKER_WORKER_TOKEN_FILE", "SSH_AUTH_SOCK"} & codex["env"].keys()


def test_generator_refuses_overwrite_and_outside_destination(tmp_path):
    project = generated(tmp_path)
    before = (project / "PROMPT.md").read_bytes()
    with pytest.raises(ValueError):
        kit.create("00", project, "http://localhost", "/tmp/project-token", sys.executable)
    assert (project / "PROMPT.md").read_bytes() == before
    with pytest.raises(ValueError):
        kit._destination(ROOT / "examples/should-not-exist")
    symlink = tmp_path / "outside"
    symlink.symlink_to(ROOT / "examples", target_is_directory=True)
    with pytest.raises(ValueError):
        kit._destination(symlink / "should-not-exist")


def test_create_cli_needs_no_project_id_and_stays_offline(tmp_path):
    destination = tmp_path / "cli-project"
    result = subprocess.run([sys.executable, str(ROOT / "tools/agent_integration.py"), "create", "--case", "00",
        "--destination", str(destination), "--broker-url", "https://offline.invalid",
        "--project-token-file", str(tmp_path / "does-not-exist.key"), "--python", sys.executable],
        capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    env = json.loads((destination / ".mcp.json").read_text())["mcpServers"]["compute_relay"]["env"]
    assert "BROKER_PROJECT_ID" not in env
    assert env["BROKER_CONTROL_URL"] == "https://offline.invalid"


@pytest.mark.parametrize("url", ["https://user:pass@host.invalid", "http://remote.invalid", "https://host.invalid/path", "https://host.invalid/?key=secret", "file:///tmp/x"])
def test_generator_rejects_credential_urls_remote_plain_http_and_nonorigin_urls(url):
    with pytest.raises(ValueError):
        kit._broker_url(url)


@pytest.mark.parametrize("case", ["00", "01", "02", "03", "04"])
def test_every_numbered_case_is_reproducible_and_explicit_about_extension_gates(tmp_path, case):
    project = generated(tmp_path, case)
    config = json.loads((project / "case.json").read_text())
    assert config["id"] == case
    assert config["required_extensions"] == (["workflows"] if case == "03" else [])
    assert config["optional_profiles"]["managed_environment"]["required_extensions"] == ["environments"]
    assert config["optional_profiles"]["bounded_fanout"]["required_extensions"] == ["workflows"]
    template = json.loads((project / "record.template.json").read_text())
    schema = json.loads((project / "record.schema.json").read_text())
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(template)
    assert template["status"] == "not_run" and not template["executions"]
    assert {check["id"] for check in template["checks"]} == {check["id"] for check in config["rubric"]}
    assert (project / "PROMPT.md").stat().st_size > 1000


def report_for(project):
    report = json.loads((project / "record.template.json").read_text())
    report["status"] = "blocked"
    for check in report["checks"]:
        check.update(status="blocked", detail="Synthetic test; no real agent or cluster was run.")
    return report


def test_recorder_stores_explicit_blocked_evidence_without_overwriting(tmp_path):
    project = generated(tmp_path)
    report = report_for(project)
    (project / "report.json").write_text(json.dumps(report))
    path = kit.record(project, "report.json", "trial-1")
    assert json.loads(path.read_text())["status"] == "blocked"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    with pytest.raises(FileExistsError):
        kit.record(project, "report.json", "trial-1")
    report["notes"] = ["cbp_" + "SYNTHETIC" * 8]
    (project / "report.json").write_text(json.dumps(report))
    with pytest.raises(ValueError):
        kit.record(project, "report.json", "trial-2")


def test_recorder_cannot_turn_missing_or_fake_execution_into_pass(tmp_path):
    project = generated(tmp_path)
    report = report_for(project)
    report["status"] = "passed"
    report["client"] = {"name": "synthetic-client", "version": "test", "model": "synthetic"}
    for check in report["checks"]:
        check["status"] = "pass"
    report["usage"] = {key: 0 for key in report["usage"]}
    (project / "report.json").write_text(json.dumps(report))
    with pytest.raises(ValueError, match="required execution evidence"):
        kit.record(project, "report.json", "missing")
    report["executions"] = [{"job_id": str(uuid4()), "attempt_id": str(uuid4()), "native_job_id": "17", "state": "succeeded", "accounting_state": "settled", "backend": "fake"}]
    (project / "report.json").write_text(json.dumps(report))
    with pytest.raises(ValueError, match="native Slurm"):
        kit.record(project, "report.json", "simulation")


def test_recorder_accepts_complete_local_evidence_without_claiming_cluster_work(tmp_path):
    project = generated(tmp_path, "01")
    report = report_for(project)
    report["status"] = "passed"
    report["client"] = {"name": "synthetic-client", "version": "test", "model": "synthetic"}
    for check in report["checks"]:
        check["status"] = "pass"
    report["usage"] = {key: 0 for key in report["usage"]}
    report["usage"]["wall_seconds"] = 0.01
    (project / "report.json").write_text(json.dumps(report))
    assert json.loads(kit.record(project, "report.json", "local-record").read_text())["executions"] == []


def test_operator_client_extensions_are_explicit_and_disabled_by_default(tmp_path):
    project = generated(tmp_path)
    env = json.loads((project / ".mcp.json").read_text())["mcpServers"]["compute_relay"]["env"]
    assert env["BROKER_EXTENSIONS"] == ""
    other = kit.create("03", tmp_path / "workflow-project", "https://broker.invalid", "/tmp/nonexistent-project-token", sys.executable, "workflows,environments")
    enabled = json.loads((other / ".mcp.json").read_text())["mcpServers"]["compute_relay"]["env"]
    assert enabled["BROKER_EXTENSIONS"] == "environments,workflows"
    assert json.loads((other / "project.json").read_text())["requested_client_extensions"] == ["environments", "workflows"]


def test_heat_has_exact_discrete_amplification_and_is_stable(science):
    small, _, _ = science
    result = small.heat()
    assert result["max_discrete_error"] < 1e-12
    assert abs(result["spatial_mean"]) < 1e-14
    assert 0 < result["discrete_amplitude"] <= 1
    assert abs(result["continuum_amplitude"] - result["discrete_amplitude"]) < 1e-4
    with pytest.raises(ValueError):
        small.heat(ratio=0.51)


def test_ou_is_seeded_and_aggregation_uses_sufficient_statistics(science, tmp_path):
    small, _, _ = science
    first = small.ou(samples=50, steps=5, seed=17)
    assert first == small.ou(samples=50, steps=5, seed=17)
    assert first != small.ou(samples=50, steps=5, seed=18)
    paths = []
    for seed in (17, 18):
        path = tmp_path / f"replica-{seed}.json"
        small.write_result(path, small.ou(samples=50, steps=5, seed=seed))
        paths.append(path)
    combined = small.aggregate(paths)
    assert combined["samples"] == 100 and combined["standard_error"] > 0
    with pytest.raises(ValueError):
        small.aggregate([paths[0], paths[0]])


def test_three_stage_science_preserves_artifact_hash_provenance(science, tmp_path):
    small, _, _ = science
    data, model = tmp_path / "data.json", tmp_path / "model.json"
    small.write_result(data, small.generate_data())
    fitted = small.fit(data)
    small.write_result(model, fitted)
    checked = small.validate_model(model)
    assert fitted["input_sha256"] == small.read_result(data)[1]
    assert checked["model_sha256"] == small.read_result(model)[1]
    assert abs(fitted["slope"] - 1.7) < 0.1
    assert checked["mse"] < 0.04


def test_dense_mean_field_analytic_limits_and_quadrature_resolution(science):
    _, dense, _ = science
    assert dense.mean_field_step(0.8, 0, 1, 1, 1) == 0
    assert dense.mean_field_step(0.3, 0.8, 0.6, 0, 0.1) == math.tanh(0.8 * (0.6 * 0.3 + 0.1))
    assert abs(dense.mean_field_step(0, 1, 1, 0.7, 0)) < 1e-15
    args = (0.3, 0.8, 0.6, 0.5, 0.15)
    assert abs(dense.mean_field_step(*args, intervals=512) - dense.mean_field_step(*args, intervals=1024)) < 1e-12


def test_tiny_disorder_is_reproducible_and_drawn_once_per_directed_edge(science, monkeypatch):
    _, dense, _ = science
    original = dense.random.Random
    calls = []

    def counted(seed):
        rng = original(seed)
        gauss = rng.gauss
        def one(*args):
            calls.append(seed)
            return gauss(*args)
        rng.gauss = one
        return rng

    monkeypatch.setattr(dense.random, "Random", counted)
    result = dense.simulate_tiny()
    assert len(calls) == dense.TINY["disorders"] * dense.TINY["n"] * (dense.TINY["n"] - 1)
    assert result == dense.simulate_tiny()
    assert result["quenched"] and not result["raw_matrix_written"]


def feasible(dense):
    p = {**dense.TINY, "n": 50000, "replicas": 2, "disorders": 2, "threads": 2}
    pilot = {"dtype": "float32", "threads": 2, "matrix_elements_per_second": 10**10,
             "matmul_flops_per_second": 10**11, "thermal_updates_per_second": 10**9}
    local = {"supported": True, "cgroup_resolution_complete": True, "effective_limit_bytes": 8 * 1024**3,
             "effective_available_bytes": 6 * 1024**3}
    grants = {"memory_mib": 16384, "storage_available_bytes": 2 * 1024**2, "cpu_core_seconds_available": 10000,
              "wall_seconds": 120, "maximum_flops": 10**13}
    return p, pilot, local, grants


def test_dense_preflight_is_arithmetic_only_and_accounts_for_all_work(science, monkeypatch):
    _, dense, _ = science
    monkeypatch.setattr(dense, "_numpy", lambda *_: pytest.fail("Preflight must not import/allocate NumPy"))
    p, pilot, local, grants = feasible(dense)
    plan = dense.preflight(p, pilot, local, grants)
    assert plan["costs"]["matrix_bytes"] > local["effective_limit_bytes"]
    assert plan["costs"]["peak_bytes"] <= grants["memory_mib"] * 1024**2
    assert plan["reserved_cpu_core_seconds"] == 240
    assert plan["costs"]["matmul_flops"] == 2 * p["n"]**2 * p["replicas"] * p["steps"] * p["disorders"]
    with pytest.raises(ValueError):
        dense.simulate_numpy(p)  # Cannot bypass the explicit resource plan through the public helper.


@pytest.mark.parametrize("reason", ["fits_local", "unknown_cgroup", "ram", "storage", "cpu", "wall", "flops", "pilot"])
def test_dense_preflight_rejects_infeasible_or_unverified_plans_before_allocation(science, reason):
    _, dense, _ = science
    p, pilot, local, grants = feasible(dense)
    if reason == "fits_local": local["effective_limit_bytes"] = 2**40
    elif reason == "unknown_cgroup": local["cgroup_resolution_complete"] = False
    elif reason == "ram": grants["memory_mib"] = 128
    elif reason == "storage": grants["storage_available_bytes"] = 1
    elif reason == "cpu": grants["cpu_core_seconds_available"] = 1
    elif reason == "wall": grants["wall_seconds"] = 61
    elif reason == "flops": grants["maximum_flops"] = 1
    else: pilot["matmul_flops_per_second"] = 1
    with pytest.raises(ValueError):
        dense.preflight(p, pilot, local, grants)


def proc_fixture(tmp_path, version=2):
    proc, mount = tmp_path / "proc", tmp_path / "cgroup"
    (proc / "self").mkdir(parents=True)
    (mount / "parent/child").mkdir(parents=True)
    (proc / "meminfo").write_text(f"MemTotal: {16 * 1024**2} kB\nMemAvailable: {10 * 1024**2} kB\n")
    if version == 2:
        (proc / "self/cgroup").write_text("0::/parent/child\n")
        (proc / "self/mountinfo").write_text(f"1 0 0:1 / {mount} rw - cgroup2 cgroup rw\n")
        for folder, cap, used in ((mount, "max", 0), (mount / "parent", 8 * 1024**3, 2 * 1024**3), (mount / "parent/child", 12 * 1024**3, 1024**3)):
            (folder / "memory.max").write_text(str(cap))
            (folder / "memory.current").write_text(str(used))
    else:
        (proc / "self/cgroup").write_text("4:memory:/parent/child\n")
        (proc / "self/mountinfo").write_text(f"1 0 0:1 / {mount} rw - cgroup cgroup rw,memory\n")
        (mount / "parent/child/memory.limit_in_bytes").write_text(str(4 * 1024**3))
        (mount / "parent/child/memory.usage_in_bytes").write_text(str(1024**3))
    return proc


@pytest.mark.parametrize("version,limit,headroom", [(2, 8, 6), (1, 4, 3)])
def test_effective_ram_respects_process_cgroup_and_ancestor_headroom(science, tmp_path, version, limit, headroom):
    _, _, resources = science
    observed = resources.effective_memory(proc_fixture(tmp_path, version))
    assert observed["supported"] and observed["cgroup_resolution_complete"]
    assert observed["effective_limit_bytes"] == limit * 1024**3
    assert observed["effective_available_bytes"] == headroom * 1024**3


def test_scripts_have_no_default_science_execution_or_implicit_large_allocation(science, tmp_path):
    _, dense, _ = science
    result = subprocess.run([sys.executable, str(COMMON / "dense_ising.py")], cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 0 and "usage:" in result.stdout
    assert not list(tmp_path.iterdir())
    bad = subprocess.run([sys.executable, str(COMMON / "dense_ising.py"), "run", "--output", str(tmp_path / "large.json")], capture_output=True)
    assert bad.returncode != 0 and not (tmp_path / "large.json").exists()


def test_numpy_tiny_variant_if_optional_dependency_is_installed(science):
    pytest.importorskip("numpy")
    _, dense, _ = science
    first = dense.simulate_numpy(dense.TINY)
    assert first == dense.simulate_numpy(dense.TINY)
    assert first["metadata"]["numpy_version"]
    assert not first["raw_matrix_written"]
