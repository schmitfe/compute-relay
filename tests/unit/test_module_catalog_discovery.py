"""Automatic site catalogs use system path data, never shell/module evaluation."""

import os
from pathlib import Path

import pytest

from compute_broker.remote import helper
from test_backends import metadata_module_catalog


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
    for index in range(20):
        directory = root / str(index);directory.mkdir();(directory / "tool.lua").touch();paths.append(str(directory))
    system_configuration(monkeypatch, {"/etc/profile": "MODULEPATH=" + ":".join(paths)})
    result = helper.automatic_module_inventory({})
    assert len(result["path_provenance"]) == 16 and result["truncated"] and result["partial"]


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
