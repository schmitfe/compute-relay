"""Tests for environment execution."""
from test_support.unit_test_backends import REVISION, sample
import json
import pytest
from compute_relay.backends import BackendError, SlurmBackend, prepare, verify_package
from compute_relay.backends.environments import EnvironmentProfileError, validate_environment_config
from compute_relay.remote import helper
from test_support.unit_test_backends import package


def test_modules_use_loaded_interpreter_and_bind_the_exact_wrapper(sample):
    request, environment, connection, _ = sample
    environment.update(kind="modules", interpreter="python3", modules=["Python/3.12.1", "CUDA/12.4"],
                       module_init="/usr/share/lmod/lmod/init/bash")
    prepared, files = package(sample)
    wrapper = prepared["generated_files"]["_broker/environment.sh"]
    assert "if ! command -v module" in wrapper
    assert "  . /usr/share/lmod/lmod/init/bash\nfi\n" in wrapper
    assert "module purge\nmodule load Python/3.12.1 CUDA/12.4\n" in wrapper
    assert prepared["generated_files"]["_broker/batch.sh"].startswith("#!/bin/bash -l\n")
    assert "exec python3 ./main.py\n" in prepared["generated_files"]["_broker/batch.sh"]
    verify_package(prepared["manifest"], prepared["manifest_sha256"], files)
    environment["modules"] = ["Python/3.13.0"]
    assert prepare(request, environment, connection, REVISION)["manifest_sha256"] != prepared["manifest_sha256"]


@pytest.mark.parametrize("change", [
    {"module_init": "/tmp/user-modules.sh"}, {"modules": ["Python/../bad"]},
    {"modules": ["Python/3.12;id"]}, {"modules": ["-t"]}, {"interpreter": "../python3"},
])
def test_modules_cannot_inject_shell_or_initialize_project_scripts(change):
    config = {"kind": "modules", "interpreter": "python3", "modules": ["Python/3.12"], **change}
    with pytest.raises(EnvironmentProfileError):
        validate_environment_config(config)


@pytest.mark.parametrize("kind", ["venv", "poetry"])
def test_existing_virtual_environments_use_their_interpreter_without_project_hooks(sample, kind):
    import shlex

    prefix = "/approved/envs/space and ' quote"
    sample[1].update(kind=kind, prefix=prefix, interpreter=prefix + "/bin/python")
    prepared, _ = package(sample)
    wrapper = prepared["generated_files"]["_broker/environment.sh"]
    assert "activate" not in wrapper
    assert "poetry run" not in wrapper
    assert shlex.split(wrapper.split("export VIRTUAL_ENV=", 1)[1].splitlines()[0]) == [prefix]
    command = prepared["generated_files"]["_broker/batch.sh"].split("exec ", 1)[1].strip()
    assert shlex.split(command) == [prefix + "/bin/python", "./main.py"]


def test_conda_execution_is_an_approved_prefix_command_in_the_compute_package(sample):
    import shlex

    sample[1].update(kind="conda", prefix="/opt/conda/envs/analysis", interpreter="/opt/conda/envs/analysis/bin/python",
                     conda_executable="/opt/conda/bin/conda")
    prepared, _ = package(sample)
    command = prepared["generated_files"]["_broker/batch.sh"].split("exec ", 1)[1].strip()
    assert shlex.split(command) == ["/opt/conda/bin/conda", "run", "--no-capture-output", "--prefix",
                                  "/opt/conda/envs/analysis", "/opt/conda/envs/analysis/bin/python", "./main.py"]
    assert "CONDA_NO_PLUGINS=true" in prepared["generated_files"]["_broker/environment.sh"]
    sample[1]["interpreter"] = "/usr/bin/python3"
    with pytest.raises(BackendError, match="selected prefix"):
        package(sample)


def test_metadata_discovery_never_executes_candidate_python_or_poetry(tmp_path, monkeypatch):
    root = tmp_path / "scoped-envs"
    prefix = root / "analysis"
    (prefix / "bin").mkdir(parents=True)
    marker = tmp_path / "unexpected-execution"
    executable = prefix / "bin" / "python"
    executable.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
    executable.chmod(0o755)
    (prefix / "pyvenv.cfg").write_text("home = /usr/bin\nversion = 3.12.2\n")
    # Never follow a sibling directory symlink into another project or home.
    (root / "outside-link").symlink_to(tmp_path, target_is_directory=True)
    monkeypatch.setattr(helper, "MODULE_INITIALIZERS", ())
    monkeypatch.setattr(helper, "software_command", lambda *_args, **_kwargs: pytest.fail("metadata discovery executed a command"))
    snapshot = helper.software_discovery({"environment_discovery": {"python_executables": [str(executable)],
        "search_roots": [{"path": str(root), "kind": "poetry", "max_depth": 2}]}})
    assert not marker.exists()
    assert snapshot["scope"] == "login_node" and snapshot["verified_on_compute"] is False
    assert snapshot["search"]["visited_directories"] == 2
    assert snapshot["environments"] == [{"kind": "poetry", "prefix": str(prefix), "interpreter": str(executable),
        "source": "configured_root_metadata", "verified_on_compute": False, "python_version": "3.12.2"}]


def test_software_probe_output_and_runtime_are_bounded():
    result = helper.software_command(["/bin/sh", "-c", "while :; do printf 'xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx'; done"], timeout=1)
    assert result["truncated"] is True
    assert len(result["stdout"].encode()) == 65536
    assert result["returncode"] != 0
    result = helper.software_command(["/bin/sleep", "10"], timeout=0.05)
    assert result["truncated"] is True


def test_module_terse_output_keeps_names_and_ignores_prose_and_directory_headers():
    text = "/opt/modulefiles:\nPython/\nPython/3.12(D)\nCUDA/12.4\ngcc\nSome explanatory prose\n../malicious\n"
    assert helper.available_modules(text) == ["CUDA/12.4", "Python/3.12", "gcc"]


@pytest.mark.parametrize("options", [
    {"shell": "id"}, {"module_init": "/tmp/modules.sh"},
    {"search_roots": [{"path": "/home", "kind": "venv"}]},
    {"search_roots": [{"path": "/opt/venvs", "max_depth": 20}]},
    {"python_executables": ["python3"]}, {"conda_executable": "/bin/bash"},
])
def test_discovery_rejects_unbounded_or_arbitrary_command_configuration(options):
    snapshot = helper.software_discovery({"environment_discovery": options})
    assert snapshot["status"] == "invalid_configuration"


def test_conda_inventory_is_fixed_and_does_not_use_personal_registries(tmp_path, monkeypatch):
    prefix = tmp_path / "conda-root" / "env1"
    (prefix / "bin").mkdir(parents=True)
    executable = prefix / "bin/python"
    executable.write_text("never executed")
    executable.chmod(0o755)
    calls = []

    def command(argv, **kwargs):
        calls.append((argv, kwargs))
        return {"returncode": 0, "stdout": json.dumps({"envs": [str(prefix)]}), "stderr": "", "truncated": False}

    monkeypatch.setattr(helper, "MODULE_INITIALIZERS", ())
    monkeypatch.setattr(helper, "software_command", command)
    snapshot = helper.software_discovery({"environment_discovery": {"python_executables": [],
        "conda_executable": "/opt/conda/bin/conda", "search_roots": [{"path": str(prefix.parent), "kind": "conda"}]}})
    assert calls == [(["/opt/conda/bin/conda", "env", "list", "--json"], {"extra_env": {"CONDA_ENVS_PATH": str(prefix.parent)}})]
    assert snapshot["conda"]["prefixes"] == [str(prefix)]
    assert snapshot["environments"][0]["interpreter"] == str(executable)


def test_discovery_config_is_forwarded_without_ssh_credentials():
    class CapturingTransport:
        def call_helper(self, source, request):
            assert "identity_file" not in repr(request)
            assert request["config"]["environment_discovery"] == {"python_executables": []}
            return {"software": {"status": "available", "scope": "login_node", "verified_on_compute": False}}

    result = SlurmBackend({"ssh": {"identity_file": "/private/key"}, "environment_discovery": {"python_executables": []}},
                          transport=CapturingTransport()).discover()
    assert result["software"]["verified_on_compute"] is False


def test_environment_search_stops_at_its_directory_budget(tmp_path, monkeypatch):
    root = tmp_path / "many-envs"
    root.mkdir()
    for number in range(270):
        (root / str(number)).mkdir()
    monkeypatch.setattr(helper, "MODULE_INITIALIZERS", ())
    result = helper.software_discovery({"environment_discovery": {"python_executables": [],
        "search_roots": [{"path": str(root), "kind": "venv", "max_depth": 3}]}})
    assert result["search"]["visited_directories"] <= 256
    assert result["search"]["truncated"] is True


def test_software_probe_scrubs_personal_startup_and_conda_plugin_environment(monkeypatch):
    monkeypatch.setenv("HOME", "/secret/personal-home")
    monkeypatch.setenv("BASH_ENV", "/secret/startup")
    monkeypatch.setenv("PYTHONPATH", "/secret/plugin-path")
    result = helper.software_command(["/usr/bin/env"])
    env = dict(line.split("=", 1) for line in result["stdout"].splitlines())
    assert env["HOME"] == "/nonexistent"
    assert env["CONDA_NO_PLUGINS"] == "true"
    assert env["CONDARC"] == "/dev/null"
    assert "BASH_ENV" not in env and "PYTHONPATH" not in env


def test_default_module_paths_are_validated_and_supplied_to_the_fixed_probe(monkeypatch):
    observed = []
    trusted = {"/etc/modulefiles", "/usr/share/lmod/lmod/modulefiles"}

    def audit(path, budget):
        observed.append(path)
        if path not in trusted:
            raise FileNotFoundError(path)

    def command(argv, **kwargs):
        assert argv[:4] == ["/bin/bash", "--noprofile", "--norc", "-c"]
        assert kwargs["extra_env"]["MODULEPATH"] == "/etc/modulefiles:/usr/share/lmod/lmod/modulefiles"
        assert "export MODULEPATH=/etc/modulefiles:/usr/share/lmod/lmod/modulefiles; module --terse avail" in argv[4]
        return {"returncode": 0, "stdout": "Python/3.12\n", "stderr": "", "truncated": False}

    monkeypatch.setattr(helper, "trusted_module_tree", audit)
    monkeypatch.setattr(helper, "trusted_system_path", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(helper, "MODULE_INITIALIZERS", ("/usr/share/lmod/lmod/init/bash",))
    monkeypatch.setattr(helper, "software_command", command)
    snapshot = helper.software_discovery({"environment_discovery": {"python_executables": []}})
    assert observed == list(helper.SYSTEM_MODULE_PATHS)
    assert snapshot["modules"]["available"] == ["Python/3.12"]
    assert snapshot["modules"]["module_paths"] == ["/etc/modulefiles", "/usr/share/lmod/lmod/modulefiles"]
    assert snapshot["modules"]["path_source"] == "automatic"


def test_explicit_module_paths_replace_defaults_and_preserve_configuration_order(monkeypatch):
    audited = []
    monkeypatch.setattr(helper, "trusted_module_tree", lambda path, _budget: audited.append(path))
    options = helper.discovery_config({"environment_discovery": {"module_paths": ["/apps/modules/Core", "/apps/modules/Linux"]}})
    paths, skipped = helper.effective_module_paths(options)
    assert paths == ["/apps/modules/Core", "/apps/modules/Linux"]
    assert audited == paths and skipped == []


def test_explicit_user_writable_module_path_is_rejected_without_executing_module_code(tmp_path, monkeypatch):
    unsafe = tmp_path / "modulefiles"
    unsafe.mkdir()
    (unsafe / ".modulerc.lua").write_text("must not execute")
    monkeypatch.setattr(helper, "software_command", lambda *_args, **_kwargs: pytest.fail("unsafe module code ran"))
    snapshot = helper.software_discovery({"environment_discovery": {"module_paths": [str(unsafe)]}})
    assert snapshot["status"] == "invalid_configuration"


def test_module_path_trust_check_rejects_writable_ancestors(monkeypatch):
    import stat
    from types import SimpleNamespace

    def info(path):
        return SimpleNamespace(st_uid=0, st_mode=stat.S_IFDIR | (0o777 if path == "/opt" else 0o755))

    monkeypatch.setattr(helper.os, "lstat", info)
    monkeypatch.setattr(helper.os, "stat", info)
    monkeypatch.setattr(helper.os.path, "realpath", lambda path: path)
    with pytest.raises(ValueError, match="root-owned and non-writable"):
        helper.trusted_system_path("/opt/modulefiles", directory=True)


def test_empty_safe_module_paths_report_that_site_configuration_is_needed(monkeypatch):
    monkeypatch.setattr(helper, "trusted_module_tree", lambda *_args: (_ for _ in ()).throw(FileNotFoundError()))
    monkeypatch.setattr(helper, "trusted_system_path", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(helper, "MODULE_INITIALIZERS", ("/usr/share/lmod/lmod/init/bash",))
    monkeypatch.setattr(helper, "software_command", lambda *_args, **_kwargs: pytest.fail("module probe without safe paths"))
    snapshot = helper.software_discovery({"environment_discovery": {"python_executables": []}})
    assert snapshot["modules"]["status"] == "configuration_required"
    assert snapshot["modules"]["module_paths"] == []
    assert any("site module_paths" in note for note in snapshot["notes"])


def test_module_environment_binds_the_selected_path_before_and_after_initialization(sample):
    import shlex

    sample[1].update(kind="modules", interpreter="python3", modules=["Python/3.12"],
                     module_paths=["/apps/modulefiles/Core", "/apps/modulefiles/path with ' quote"], module_init="/etc/profile.d/modules.sh")
    prepared, files = package(sample)
    lines = prepared["generated_files"]["_broker/environment.sh"].splitlines()
    exports = [index for index, line in enumerate(lines) if line.startswith("export MODULEPATH=")]
    initialization = lines.index("  . /etc/profile.d/modules.sh")
    assert len(exports) == 2 and exports[0] < initialization < lines.index("module purge") < exports[1] < lines.index("module load Python/3.12")
    joined_path = ":".join(sample[1]["module_paths"])
    assert shlex.split(lines[exports[0]].split("=", 1)[1]) == [joined_path]
    verify_package(prepared["manifest"], prepared["manifest_sha256"], files)
    sample[1]["module_paths"] = ["/apps/different-modulefiles"]
    assert package(sample)[0]["manifest_sha256"] != prepared["manifest_sha256"]


@pytest.mark.parametrize("paths", [[], "bad", ["relative/path"], ["/apps/a:/home/user/modules"], ["/"], ["/apps/a\nunsafe"], ["/apps/a"] * 17])
def test_module_environment_rejects_invalid_path_lists(paths):
    with pytest.raises(EnvironmentProfileError):
        validate_environment_config({"kind": "modules", "interpreter": "python3", "module_paths": paths})


def test_nonmodule_profile_cannot_silently_ignore_module_path_selection():
    with pytest.raises(EnvironmentProfileError, match="Choose a module or virtual environment profile"):
        validate_environment_config({"kind": "system", "interpreter": "/usr/bin/python3", "module_paths": ["/apps/modules"]})
