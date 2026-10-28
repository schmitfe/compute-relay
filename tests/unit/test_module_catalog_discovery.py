"""Automatic site catalogs use system path data, never shell/module evaluation."""

import os
from pathlib import Path

import pytest

from compute_relay.remote import helper
from test_support.unit_test_backends import metadata_module_catalog


def system_configuration(monkeypatch, documents):
    reads = []
    def read(filename, maximum):
        reads.append(filename)
        if filename not in documents:
            raise FileNotFoundError(filename)
        assert len(documents[filename].encode()) <= maximum
        return documents[filename]
    monkeypatch.setattr(helper, "read_trusted_module_configuration", read)
    monkeypatch.setattr(helper, "trusted_system_path", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(helper, "SYSTEM_MODULE_PATHS", ())
    monkeypatch.setattr(helper, "MODULE_PROFILE_DIRECTORIES", ())
    monkeypatch.setattr(helper, "MODULE_INITIALIZERS", ("/usr/share/lmod/lmod/init/bash",))
    monkeypatch.setattr(helper, "software_command", lambda *_args, **_kwargs: pytest.fail("automatic catalog executed a command"))
    return reads


def test_literal_modulepath_parser_never_interprets_shell_or_variables():
    text = '''export MODULEPATH="/opt/apps/modules/Core:/opt/apps/modules/Software"
export MODULEPATH="$(touch /tmp/must-not-run)"
export MODULEPATH=$MODULEPATH:/private/modules
export OTHER_VARIABLE=/private/secret
LMOD_MODULEPATH_INIT=/etc/lmod/.modulespath
'''
    paths, references, unsupported = helper.literal_module_paths(text)
    assert paths == ["/opt/apps/modules/Core", "/opt/apps/modules/Software"]
    assert references == ["/etc/lmod/.modulespath"] and unsupported == 2


def test_auto_catalog_recovers_site_module_roots_from_system_profile_metadata(metadata_module_catalog, monkeypatch):
    root = metadata_module_catalog
    paths = []
    for category, name in (("Core", "python3/3.12.2.lua"), ("Software", "python3/3.10.lua"),
            ("Tools", "cmake/3.29.lua"), ("Suits", "scientific/2024.tcl")):
        directory = root / category;path = directory / name;path.parent.mkdir(parents=True)
        path.write_text("os.execute('must-not-run')")
        (directory / ".modulerc.lua").write_text("must-not-read")
        paths.append(str(directory))
    alias = root.parent / "site-alias";alias.symlink_to(root, target_is_directory=True)
    configured = [str(alias / Path(path).name) for path in paths]
    reads = system_configuration(monkeypatch, {"/etc/profile": 'export MODULEPATH="' + ":".join(configured) + '"'})
    monkeypatch.setattr(helper, "read_file", lambda *_args, **_kwargs: pytest.fail("module file contents were read"))
    result = helper.software_discovery({"environment_discovery": {"module_inventory_mode": "auto", "python_executables": []}})
    modules = result["modules"]
    assert modules["available"] == ["cmake/3.29", "python3/3.10", "python3/3.12.2", "scientific/2024"]
    assert modules["module_paths"] == paths
    assert modules["requested_inventory_mode"] == "auto" and modules["inventory_mode"] == "metadata" and modules["provisional"]
    assert modules["loaded"] == [] and modules["loaded_state"] == "not_observed"
    assert all(item["source"] == "/etc/profile" for item in modules["path_provenance"])
    assert modules["init_path"] == "/usr/share/lmod/lmod/init/bash"
    assert "System shell startup" in modules["explanation"] and "/etc/profile" in reads


def test_standard_modulespath_file_supports_bounded_final_directory_wildcard(metadata_module_catalog, monkeypatch):
    root = metadata_module_catalog
    (root / "Core/python3").mkdir(parents=True);(root / "Core/python3/3.13.lua").touch()
    (root / "Tools").mkdir();(root / "Tools/tool.lua").touch()
    (root / "alias").symlink_to(root / "Core", target_is_directory=True)
    system_configuration(monkeypatch, {"/etc/lmod/.modulespath": str(root) + "/* # site categories\n"})
    result = helper.software_discovery({"environment_discovery": {"module_inventory_mode": "auto", "python_executables": []}})
    assert result["modules"]["available"] == ["python3/3.13", "tool"]
    assert all(not path.endswith("/alias") for path in result["modules"]["module_paths"])


def test_auto_catalog_reads_site_profile_snippet_without_shell_execution(metadata_module_catalog, monkeypatch):
    root = metadata_module_catalog
    (root / "Python").mkdir();(root / "Python/3.13.lua").write_text("os.execute('must-not-run')")
    snippets = root.parent / "profile.d";snippets.mkdir()
    (snippets / "site-software.sh").touch()
    (snippets / "site-software.csh").touch()
    (snippets / ".personal.sh").touch()
    filename = str(snippets / "site-software.sh")
    reads = system_configuration(monkeypatch, {filename: "export MODULEPATH=" + str(root)})
    monkeypatch.setattr(helper, "MODULE_PROFILE_DIRECTORIES", (str(snippets),))
    result = helper.automatic_module_inventory({})
    assert result["available"] == ["Python/3.13"]
    assert filename in reads and str(snippets / "site-software.csh") not in reads
    assert result["configuration_directories"] == [{"path": str(snippets), "status": "listed"}]
    assert result["path_provenance"][0]["source"] == filename


def test_auto_catalog_follows_literal_system_module_init_references(metadata_module_catalog, monkeypatch):
    root = metadata_module_catalog
    (root / "Python").mkdir();(root / "Python/3.12.lua").touch()
    reads = system_configuration(monkeypatch, {
        "/etc/profile": ". /opt/site/lmod/init/profile\nsource /home/user/init/bash\nsource $(touch /tmp/nope)",
        "/opt/site/lmod/init/profile": "LMOD_MODULEPATH_INIT=/opt/site/etc/modulespath",
        "/opt/site/etc/modulespath": str(root),
    })
    result = helper.automatic_module_inventory({})
    assert result["available"] == ["Python/3.12"]
    assert "/opt/site/lmod/init/profile" in reads and "/opt/site/etc/modulespath" in reads
    assert "/home/user/init/bash" not in reads


def test_auto_catalog_reads_only_literal_initrc_module_use_paths(metadata_module_catalog, monkeypatch):
    root = metadata_module_catalog
    (root / "tool.lua").touch()
    reads = system_configuration(monkeypatch, {
        "/etc/environment-modules/initrc": "module use --append " + str(root) + "\nmodule load hostile-default\nmodule use [exec touch /tmp/nope]"
    })
    result = helper.automatic_module_inventory({})
    assert result["available"] == ["tool"] and "/etc/environment-modules/initrc" in reads
    assert result["partial"] and result["unsupported_configuration_lines"]


def test_site_module_use_union_keeps_conditional_paths_without_evaluating_unuse(metadata_module_catalog, monkeypatch):
    root = metadata_module_catalog
    ramses = root / "ramses/modules";ramses.mkdir(parents=True)
    noeb = root / "noeb/arch/zen4/modules";noeb.mkdir(parents=True)
    easybuild = root / "eb/arch/zen4/modules"
    for path, name in ((ramses, "Python/3.12.lua"), (noeb, "custom/1.0"),
                       (easybuild / "all", "NumPy/2.0.lua"), (easybuild / "bio", "BioPython/1.0.lua")):
        (path / name).parent.mkdir(parents=True, exist_ok=True);(path / name).touch()
    snippets = root.parent / "profile.d";snippets.mkdir()
    for name in ("ramsesmods.sh", "zeasybuild.sh"):
        (snippets / name).touch()
    system_configuration(monkeypatch, {
        str(snippets / "ramsesmods.sh"): "module use " + str(ramses) + "\nmodule use " + str(noeb),
        str(snippets / "zeasybuild.sh"): "export MODULES_SEARCH_MATCH=contains\nif [ -n \"$HOSTTYPE\" ]; then\n module use " + str(easybuild) + "/*\n module unuse " + str(easybuild / "all") + "\nfi\n# module use /not/active/old-stack",
    })
    monkeypatch.setattr(helper, "MODULE_PROFILE_DIRECTORIES", (str(snippets),))
    result = helper.automatic_module_inventory({})
    assert result["available"] == ["BioPython/1.0", "NumPy/2.0", "Python/3.12", "custom/1.0"]
    assert result["path_semantics"] == "literal_candidate_union" and result["partial"]
    assert result["unsupported_configuration_lines"] == 1
    assert "module unuse are not interpreted" in result["explanation"]
    assert all("old-stack" not in entry["path"] for entry in result["path_provenance"])


def test_system_profile_listing_is_bounded_and_rejects_untrusted_directory(tmp_path, monkeypatch):
    snippets = tmp_path / "profile.d";snippets.mkdir()
    for index in range(60):
        (snippets / ("site%02d.sh" % index)).touch()
    monkeypatch.setattr(helper, "MODULE_PROFILE_DIRECTORIES", (str(snippets),))
    monkeypatch.setattr(helper, "trusted_system_path", lambda *_args, **_kwargs: None)
    paths, directories, truncated = helper.module_profile_configuration(helper.time.monotonic() + 2)
    assert len(paths) == 48 and truncated and directories[0]["status"] == "listed"
    def untrusted(*_args, **_kwargs): raise ValueError("untrusted")
    monkeypatch.setattr(helper, "trusted_system_path", untrusted)
    paths, directories, truncated = helper.module_profile_configuration(helper.time.monotonic() + 2)
    assert paths == [] and directories[0]["status"] == "unsafe_or_unavailable"


def test_auto_mode_ignores_shell_constructs_and_never_scans_personal_homes(metadata_module_catalog, monkeypatch):
    marker = metadata_module_catalog / "executed"
    system_configuration(monkeypatch, {"/etc/profile": 'export MODULEPATH="$(touch ' + str(marker) + ')"\nexport MODULEPATH=/home/other/*'})
    monkeypatch.setattr(helper.os, "scandir", lambda *_args: pytest.fail("personal home was scanned"))
    result = helper.software_discovery({"environment_discovery": {"module_inventory_mode": "auto", "python_executables": []}})
    assert result["modules"]["available"] == [] and not marker.exists()
    assert result["modules"]["unsupported_configuration_lines"] >= 2


def test_auto_path_configuration_budget_stops_following_new_references(monkeypatch):
    documents = {"/etc/profile": "LMOD_MODULEPATH_INIT=/etc/first/modulespath"}
    reads = system_configuration(monkeypatch, documents)
    def read(filename, maximum):
        reads.append(filename)
        return "#" * maximum
    monkeypatch.setattr(helper, "read_trusted_module_configuration", read)
    result = helper.automatic_module_inventory({})
    assert result["configuration_bytes_read"] == 131072 and result["truncated"]
    assert len(reads) == 2


def test_auto_mode_caps_candidate_roots_and_reports_truncation(metadata_module_catalog, monkeypatch):
    root = metadata_module_catalog
    paths = []
    for index in range(70):
        directory = root / str(index);directory.mkdir();(directory / "tool.lua").touch();paths.append(str(directory))
    system_configuration(monkeypatch, {"/etc/profile": "MODULEPATH=" + ":".join(paths)})
    result = helper.automatic_module_inventory({})
    assert len(result["path_provenance"]) == 64 and result["truncated"] and result["partial"]


def test_auto_root_declared_group_managed_tree_is_names_only(metadata_module_catalog, monkeypatch):
    root = metadata_module_catalog
    root.chmod(0o2775)
    (root / "Python").mkdir(mode=0o2775)
    module = root / "Python/3.13.lua";module.write_text("os.execute('must-not-run')");module.chmod(0o664)
    (root / "unsafe.lua").touch();(root / "unsafe.lua").chmod(0o666)
    (root / "alias.lua").symlink_to(module)
    system_configuration(monkeypatch, {"/etc/profile": "MODULEPATH=" + str(root)})
    result = helper.automatic_module_inventory({})
    assert result["available"] == ["Python/3.13"]
    assert result["group_writable_entries"] >= 2 and result["provisional"]
    assert result["write_policy"] == "root_declared_group_managed_metadata"
    assert result["skipped"]["unsafe_permissions"] == 1 and result["skipped"]["symlinks"] == 1
    assert any(w["reason"] == "group_managed_metadata_only" for w in result["ancestor_warnings"])
    # Neither direct metadata nor protected explicit auto paths silently gain
    # the exception reserved for system-root-declared software trees.
    assert helper.metadata_module_inventory({"module_paths": [str(root)]})["available"] == []
    assert helper.automatic_module_inventory({"module_paths": [str(root)]})["available"] == []


def test_auto_catalog_larger_than_512_names_has_explicit_fixed_bounds(metadata_module_catalog, monkeypatch):
    root = metadata_module_catalog
    for index in range(600):
        (root / ("tool%04d.lua" % index)).touch()
    system_configuration(monkeypatch, {"/etc/profile": "MODULEPATH=" + str(root)})
    result = helper.automatic_module_inventory({})
    assert len(result["available"]) == 600 and not result["truncated"]
    assert result["limits"]["names"] == 8192 and result["limits"]["entries"] == 32768
    assert result["limits"]["seconds"] == 20


def test_auto_never_reads_non_root_site_configuration(metadata_module_catalog, monkeypatch):
    root = metadata_module_catalog
    root.chmod(0o2775);(root / "hostile.lua").touch()
    system_configuration(monkeypatch, {})
    def untrusted(_filename, _maximum):
        raise ValueError("not root-owned")
    monkeypatch.setattr(helper, "read_trusted_module_configuration", untrusted)
    result = helper.automatic_module_inventory({})
    assert result["available"] == []
    assert all(source["status"] == "unsafe_unavailable_or_size_limited" for source in result["configuration_sources"])


def test_explicit_availability_mode_does_not_enable_automatic_metadata(monkeypatch):
    monkeypatch.setattr(helper, "automatic_module_inventory", lambda *_args: pytest.fail("availability policy changed"))
    monkeypatch.setattr(helper, "effective_module_paths", lambda *_args: ([], []))
    monkeypatch.setattr(helper, "MODULE_INITIALIZERS", ())
    result = helper.software_discovery({"environment_discovery": {"module_inventory_mode": "availability", "python_executables": []}})
    assert result["modules"]["inventory_mode"] == "availability" and not result["modules"]["provisional"]


def test_trusted_module_configuration_rejects_untrusted_or_oversized_file(tmp_path, monkeypatch):
    path = tmp_path / "profile";path.write_text("MODULEPATH=/apps/modules\n")
    monkeypatch.setattr(helper, "trusted_system_path", lambda *_args, **_kwargs: None)
    # Root ownership is still required even if the preceding path audit is bypassed.
    if os.getuid() != 0:
        with pytest.raises(ValueError): helper.read_trusted_module_configuration(str(path), 65536)
    with pytest.raises(ValueError): helper.read_trusted_module_configuration(str(path), 1)


def test_optional_availability_requires_boolean_owner_opt_in_for_auto_mode():
    assert helper.discovery_config({})["module_availability_enabled"] is False
    for value in ("yes", 1, None):
        with pytest.raises(ValueError):
            helper.discovery_config({"environment_discovery": {"module_inventory_mode": "auto", "module_availability_enabled": value}})
    with pytest.raises(ValueError):
        helper.discovery_config({"environment_discovery": {"module_availability_enabled": True}})
    assert helper.discovery_config({"environment_discovery": {"module_inventory_mode": "auto", "module_availability_enabled": True}})["module_availability_enabled"]


def test_opt_in_availability_uses_fixed_command_and_separate_provenance(metadata_module_catalog, monkeypatch):
    root = metadata_module_catalog
    (root / "metadata-only.lua").touch()
    system_configuration(monkeypatch, {"/etc/profile": "MODULEPATH=" + str(root)})
    calls = []
    def command(argv, **kwargs):
        calls.append((argv, kwargs))
        return {"returncode": 0, "stdout": "Python/3.13\n", "stderr": "CUDA/12.4(default)\n", "truncated": False}
    monkeypatch.setattr(helper, "software_command", command)
    m = helper.software_discovery({"environment_discovery": {"module_inventory_mode": "auto", "module_availability_enabled": True, "python_executables": []}})["modules"]
    assert len(calls) == 1 and calls[0][0][:4] == ["/bin/bash", "--noprofile", "--norc", "-c"]
    assert calls[0][0][4].endswith(" && module --terse avail")
    assert calls[0][1]["extra_env"]["MODULEPATH"] == str(root)
    assert calls[0][1]["max_output_bytes"] == 262144 and calls[0][1]["timeout"] == 8
    assert m["metadata_available"] == ["metadata-only"]
    assert m["availability"]["available"] == ["CUDA/12.4", "Python/3.13"]
    assert m["available"] == ["CUDA/12.4", "Python/3.13", "metadata-only"]
    assert m["availability"]["module_code_evaluation_authorized"] is True and m["provisional"]


def test_availability_failure_and_truncation_preserve_metadata_catalog(metadata_module_catalog, monkeypatch):
    root = metadata_module_catalog;(root / "metadata-only.lua").touch()
    system_configuration(monkeypatch, {"/etc/profile": "MODULEPATH=" + str(root)})
    monkeypatch.setattr(helper, "software_command", lambda *_args, **_kwargs: {
        "returncode": -9, "stdout": "partial-name\n", "stderr": "", "truncated": True})
    m = helper.software_discovery({"environment_discovery": {"module_inventory_mode": "auto", "module_availability_enabled": True, "python_executables": []}})["modules"]
    assert m["available"] == ["metadata-only"] and m["partial"] and m["truncated"]
    assert m["availability"]["status"] == "unavailable" and m["availability"]["truncated"]


def test_availability_requires_trusted_initializer_even_after_metadata_discovery(monkeypatch):
    def unavailable(*_args, **_kwargs): raise ValueError("untrusted initializer")
    monkeypatch.setattr(helper, "trusted_system_path", unavailable)
    monkeypatch.setattr(helper, "software_command", lambda *_args, **_kwargs: pytest.fail("untrusted initializer ran"))
    modules = {"init_path": "/etc/profile.d/modules.sh", "module_paths": ["/software/modules"],
               "available": ["metadata-only"], "warnings": [], "partial": False}
    helper.augment_module_availability(modules)
    assert modules["availability"]["status"] == "unavailable" and modules["partial"]


def test_fixed_probe_larger_output_bound_remains_bounded():
    result = helper.software_command(["/bin/sh", "-c", "while :; do printf 'xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx'; done"],
                                     timeout=1, max_output_bytes=131072)
    assert result["truncated"] and len(result["stdout"]) <= 131072
    with pytest.raises(ValueError):
        helper.software_command(["/bin/true"], max_output_bytes=262145)


def test_availability_keeps_later_validated_roots_when_metadata_scan_is_truncated(metadata_module_catalog, monkeypatch):
    root = metadata_module_catalog
    early = root / "early";early.mkdir()
    later = root / "later";later.mkdir()
    paths = [str(early), str(later)]
    system_configuration(monkeypatch, {"/etc/profile": "MODULEPATH=" + ":".join(paths)})
    original = helper.metadata_module_inventory
    def cold_scan(options):
        result = original({**options, "module_paths": options["module_paths"][:1]})
        result["truncated"] = result["partial"] = True
        return result
    monkeypatch.setattr(helper, "metadata_module_inventory", cold_scan)
    calls = []
    def command(argv, **kwargs):
        calls.append(kwargs["extra_env"]["MODULEPATH"])
        return {"returncode": 0, "stdout": "later/Python/3.13\n", "stderr": "", "truncated": False}
    monkeypatch.setattr(helper, "software_command", command)
    m = helper.software_discovery({"environment_discovery": {"module_inventory_mode": "auto",
        "module_availability_enabled": True, "python_executables": []}})["modules"]
    assert m["module_paths"] == [str(early)] and m["truncated"]
    assert m["intended_module_paths"] == paths and m["validated_module_paths"] == paths
    assert calls == [":".join(paths)] and m["availability"]["module_paths"] == paths
    assert m["availability"]["status"] == "available" and "later/Python/3.13" in m["available"]


def test_availability_refuses_incomplete_path_validation(monkeypatch):
    monkeypatch.setattr(helper, "software_command", lambda *_args, **_kwargs: pytest.fail("shortened path was used"))
    m = {"available": [], "module_paths": ["/software/first"], "validated_module_paths": ["/software/first"],
         "module_path_validation_truncated": True, "init_path": "/etc/profile.d/modules.sh", "partial": True}
    helper.augment_module_availability(m)
    assert m["availability"]["reason"] == "full_modulepath_validation_incomplete"


def test_initializer_reference_reports_site_canonical_bash_without_reading_it(metadata_module_catalog, monkeypatch):
    root = metadata_module_catalog
    init = root / "lmod/9.0/init";init.mkdir(parents=True)
    (init / "profile").write_text("must-not-read-or-source")
    (init / "bash").write_text("must-not-read-or-source")
    observed = root.parent / "z00_lmod.sh";observed.symlink_to(init / "profile")
    real_lstat, real_fstat = helper.os.lstat, helper.os.fstat
    def protected_reference(path, *args, **kwargs):
        info = real_lstat(path, *args, **kwargs)
        fields = list(info);fields[4] = 0
        if not helper.stat.S_ISLNK(info.st_mode):
            fields[0] &= ~0o022
        return os.stat_result(fields)
    def site_metadata(fd):
        info = real_fstat(fd);fields = list(info)
        if helper.stat.S_ISREG(info.st_mode): fields[4] = 1000004
        return os.stat_result(fields)
    monkeypatch.setattr(helper.os, "lstat", protected_reference)
    monkeypatch.setattr(helper.os, "fstat", site_metadata)
    def site_untrusted(*_args, **_kwargs): raise ValueError("site-owned target")
    monkeypatch.setattr(helper, "trusted_system_path", site_untrusted)
    monkeypatch.setattr(helper, "read_file", lambda *_args, **_kwargs: pytest.fail("initializer contents read"))
    monkeypatch.setattr(helper, "software_command", lambda *_args, **_kwargs: pytest.fail("initializer executed"))
    candidate = helper.module_initializer_candidate(str(observed))
    assert candidate["observed_path"] == str(observed)
    assert candidate["observed_canonical_path"] == str(init / "profile")
    assert candidate["canonical_path"] == str(init / "bash")
    assert candidate["owner_uid"] == 1000004 and not candidate["root_trusted"]
    assert candidate["verified_on_compute"] is False


def test_initializer_candidate_requires_system_owned_reference(tmp_path):
    observed = tmp_path / "z00_lmod.sh";observed.write_text("must-not-source")
    # A path beneath writable /tmp is not a protected system reference, even
    # if the test process happens to own its final file as UID0.
    with pytest.raises(ValueError): helper.module_initializer_candidate(str(observed))


def test_site_initializer_candidate_keeps_strict_availability_initializer(metadata_module_catalog, monkeypatch):
    root = metadata_module_catalog;(root / "tool.lua").touch()
    system_configuration(monkeypatch, {"/etc/profile": "MODULEPATH=" + str(root)})
    candidate = {"observed_path": "/etc/profile.d/z00_lmod.sh", "observed_canonical_path": "/site/lmod/9/init/profile",
        "canonical_path": "/site/lmod/9/init/bash", "source": "known_system_initializer_reference",
        "owner_uid": 1000004, "group_gid": 517, "mode": "0o755", "root_trusted": False, "verified_on_compute": False}
    monkeypatch.setattr(helper, "module_initializer_candidate", lambda _path: candidate)
    calls = []
    def command(argv, **_kwargs):
        calls.append(argv[4]);return {"returncode": 0, "stdout": "tool\n", "stderr": "", "truncated": False}
    monkeypatch.setattr(helper, "software_command", command)
    m = helper.software_discovery({"environment_discovery": {"module_inventory_mode": "auto",
        "module_availability_enabled": True, "python_executables": []}})["modules"]
    assert m["init_path"] == "/site/lmod/9/init/bash" and m["initializer_status"] == "site_metadata_candidate"
    assert m["observed_init_path"] == "/etc/profile.d/z00_lmod.sh" and not m["initializer_verified_on_compute"]
    assert m["availability_init_path"] == "/usr/share/lmod/lmod/init/bash"
    assert ". /usr/share/lmod/lmod/init/bash " in calls[0] and "/site/lmod/9/init/bash" not in calls[0]


def test_metadata_module_catalog_never_reads_or_executes_module_files(metadata_module_catalog, monkeypatch):
    root = metadata_module_catalog
    (root / "python3").mkdir()
    (root / "gcc").mkdir()
    (root / "python3/3.12.2.lua").write_text("os.execute('must-not-execute')")
    (root / "gcc/13.2.tcl").write_text("exec must-not-execute")
    (root / "gcc/12.1").write_text("#%Module\nexec must-not-execute")
    (root / "julia.lua").write_text("error('must-not-read')")
    for name in (".modulerc.lua", "modulerc.lua", "aliases.tcl", "default", "README", "notes.txt", "setup.py"):
        (root / name).write_text("must-not-read")
    monkeypatch.setattr(helper, "software_command", lambda *_args, **_kwargs: pytest.fail("metadata invoked command"))
    monkeypatch.setattr(helper, "read_file", lambda *_args, **_kwargs: pytest.fail("metadata read file content"))
    monkeypatch.setattr(helper, "trusted_module_tree", lambda *_args, **_kwargs: pytest.fail("metadata used availability audit"))
    # Only initializer metadata is checked; the reference can prefill a recipe.
    monkeypatch.setattr(helper, "trusted_system_path", lambda *_args, **_kwargs: None)
    result = helper.software_discovery({"environment_discovery": {"module_inventory_mode": "metadata",
        "module_paths": [str(root)], "module_init": "/usr/share/lmod/lmod/init/bash", "python_executables": []}})
    modules = result["modules"]
    assert modules["status"] == "available"
    assert modules["available"] == ["gcc/12.1", "gcc/13.2", "julia", "python3/3.12.2"]
    assert modules["inventory_mode"] == "metadata" and modules["provisional"] is True
    assert modules["source"] == "configured_module_file_metadata"
    assert "were not evaluated" in modules["explanation"]
    assert modules["init_path"] == "/usr/share/lmod/lmod/init/bash"
    assert modules["initializer_status"] == "metadata_verified"


def test_metadata_catalog_skips_symlinks_and_writable_subtrees(metadata_module_catalog, tmp_path):
    root = metadata_module_catalog
    (root / "safe.lua").write_text("no reading")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private.lua").write_text("must not inspect")
    (root / "directory_link").symlink_to(outside, target_is_directory=True)
    (root / "file_link.lua").symlink_to(outside / "private.lua")
    unsafe = root / "unsafe"
    unsafe.mkdir()
    unsafe.chmod(0o777)
    (unsafe / "injected.lua").write_text("no reading")
    writable = root / "writable.lua"
    writable.write_text("no reading")
    writable.chmod(0o666)
    result = helper.metadata_module_inventory({"module_paths": [str(root)]})
    assert result["available"] == ["safe"]
    assert result["visited_directories"] == 1
    assert result["skipped"]["symlinks"] == 2
    assert result["skipped"]["unsafe_permissions"] == 2
    assert result["partial"] is True and result["warnings"]


def test_metadata_root_rejects_symlink_alias_even_when_target_is_readable(metadata_module_catalog, tmp_path):
    alias = tmp_path / "module-alias"
    alias.symlink_to(metadata_module_catalog, target_is_directory=True)
    result = helper.metadata_module_inventory({"module_paths": [str(alias)]})
    assert result["status"] == "unavailable" and result["module_paths"] == []
    assert result["roots"][0]["status"] == "unsafe_or_inaccessible"


def test_metadata_mode_rejects_writable_ancestor_without_opening_contents(tmp_path):
    parent = tmp_path / "shared-parent"
    parent.mkdir()
    parent.chmod(0o777)
    root = parent / "modulefiles"
    root.mkdir(mode=0o755)
    with pytest.raises(ValueError, match="allows unsupported group/world writes"):
        helper.open_metadata_module_root(str(root))


@pytest.mark.parametrize("options", [
    {"module_inventory_mode": "metadata"}, {"module_inventory_mode": "metadata", "module_paths": []},
    {"module_inventory_mode": "shell"}, {"module_inventory_mode": []},
    {"module_inventory_mode": "metadata", "module_paths": ["relative/modules"]},
])
def test_metadata_configuration_requires_an_explicit_bounded_path_list(options):
    assert helper.software_discovery({"environment_discovery": options})["status"] == "invalid_configuration"


def test_default_module_inventory_keeps_availability_audit_and_semantics(monkeypatch):
    calls = []
    monkeypatch.setattr(helper, "effective_module_paths", lambda options: (calls.append(options["module_inventory_mode"]) or ([], [])))
    monkeypatch.setattr(helper, "metadata_module_inventory", lambda *_args: pytest.fail("default unexpectedly became metadata mode"))
    monkeypatch.setattr(helper, "MODULE_INITIALIZERS", ())
    result = helper.software_discovery({"environment_discovery": {"python_executables": []}})
    assert calls == ["availability"]
    assert result["modules"]["inventory_mode"] == "availability"
    assert result["modules"]["provisional"] is False


def test_metadata_catalog_bounds_names_and_leaves_later_roots_unopened(metadata_module_catalog, tmp_path, monkeypatch):
    root = metadata_module_catalog
    for number in range(520):
        (root / f"module{number}.lua").touch()
    later = tmp_path / "later-modules"
    later.mkdir()
    opened = []
    real_open = helper.open_metadata_module_root

    def record(path, **kwargs):
        opened.append(path)
        return real_open(path, **kwargs)

    monkeypatch.setattr(helper, "open_metadata_module_root", record)
    result = helper.metadata_module_inventory({"module_paths": [str(root), str(later)]})
    assert len(result["available"]) == 512
    assert result["truncated"] is True and result["partial"] is True
    assert result["roots"][1]["status"] == "not_scanned_limit"
    assert opened == [str(root)]


def test_metadata_catalog_checks_wallclock_inside_directory_iteration(metadata_module_catalog, monkeypatch):
    root = metadata_module_catalog
    (root / "a.lua").touch()
    (root / "b.lua").touch()
    clock = iter([0, 0, 0, 0, 0, 0, 0, 0, 3])
    monkeypatch.setattr(helper.time, "monotonic", lambda: next(clock, 3))
    result = helper.metadata_module_inventory({"module_paths": [str(root)]})
    assert result["truncated"] is True
    assert result["visited_entries"] <= 1


def test_metadata_catalog_limits_entry_and_directory_counts(metadata_module_catalog):
    root = metadata_module_catalog
    # Housekeeping never populates the name set, so the entry bound is exercised.
    for number in range(4100):
        (root / f".ignored{number}").touch()
    result = helper.metadata_module_inventory({"module_paths": [str(root)]})
    assert result["visited_entries"] <= 4096
    assert result["truncated"] is True


@pytest.mark.parametrize("uid,mode,final,permitted,warned", [
    (0, 0o1777, False, True, True), (0, 0o775, False, True, True),
    (0, 0o755, True, True, False), (1000004, 0o755, True, True, False),
    (0, 0o777, False, False, False), (1000004, 0o775, False, False, False),
    (1000004, 0o1777, False, False, False), (0, 0o1777, True, False, False),
    (0, 0o775, True, False, False), (1000004, 0o775, True, False, False),
])
def test_metadata_shared_ancestor_exceptions_are_narrow_and_reported(uid, mode, final, permitted, warned):
    import stat
    from types import SimpleNamespace

    info = SimpleNamespace(st_uid=uid, st_mode=stat.S_IFDIR | mode)
    warnings = []
    if permitted:
        helper.metadata_module_directory_permissions(info, "/projects", final=final, ancestor_warnings=warnings)
        assert bool(warnings) is warned
        if warned:
            assert warnings[0]["path"] == "/projects"
            assert (warnings[0]["uid"], warnings[0]["mode"]) == (0, oct(mode))
    else:
        with pytest.raises(ValueError):
            helper.metadata_module_directory_permissions(info, "/projects", final=final, ancestor_warnings=warnings)
        assert warnings == []


def test_availability_mode_does_not_inherit_metadata_sticky_ancestor_exception(monkeypatch):
    import stat
    from types import SimpleNamespace

    info = SimpleNamespace(st_uid=0, st_mode=stat.S_IFDIR | 0o1777)
    monkeypatch.setattr(helper.os, "lstat", lambda *_args, **_kwargs: info)
    monkeypatch.setattr(helper.os.path, "realpath", lambda path: path)
    with pytest.raises(ValueError, match="root-owned and non-writable"):
        helper.trusted_system_path("/projects/modulefiles", directory=True)


def test_metadata_catalog_bounds_pending_handles_and_directory_depth(metadata_module_catalog):
    root = metadata_module_catalog
    for number in range(270):
        directory = root / f"directory{number}"
        directory.mkdir()
        (directory / "version.lua").touch()
    result = helper.metadata_module_inventory({"module_paths": [str(root)]})
    assert result["visited_directories"] <= 256
    assert result["truncated"] is True


def test_metadata_shared_ancestor_warning_evidence_is_bounded():
    import stat
    from types import SimpleNamespace

    info = SimpleNamespace(st_uid=0, st_mode=stat.S_IFDIR | 0o775)
    warnings = []
    for number in range(64):
        helper.metadata_module_directory_permissions(info, f"/shared/{number}", final=False, ancestor_warnings=warnings)
    with pytest.raises(ValueError, match="warning bound"):
        helper.metadata_module_directory_permissions(info, "/shared/extra", final=False, ancestor_warnings=warnings)
    assert len(warnings) == 64
