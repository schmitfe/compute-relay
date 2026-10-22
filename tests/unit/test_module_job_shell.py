"""Module recipes use the site login shell and report missing commands clearly."""

import subprocess

import pytest

from compute_broker.backends.environments import render_environment


@pytest.mark.parametrize("initializer", ["", "/usr/share/lmod/lmod/init/bash"])
def test_available_module_command_never_sources_an_initializer(initializer):
    wrapper, command = render_environment({"kind": "modules", "interpreter": "python3",
        "modules": ["Python/3.12"], "module_init": initializer}, "python3")
    setup = 'module() { printf "module:%s\\n" "$*"; };\n'
    result = subprocess.run(["/bin/bash", "--noprofile", "--norc", "-c", setup + wrapper],
        capture_output=True, text=True, check=False, timeout=5)
    assert result.returncode == 0 and result.stderr == ""
    assert result.stdout == "module:purge\nmodule:load Python/3.12\n"
    assert command == ["python3"]
    if not initializer:
        assert ". /" not in wrapper


def test_unavailable_module_command_has_a_fixed_actionable_error(tmp_path):
    wrapper, _ = render_environment({"kind": "modules", "interpreter": "python3",
        "modules": ["Python/3.12"]}, "python3")
    result = subprocess.run(["/bin/bash", "--noprofile", "--norc", "-c", wrapper],
        env={"PATH": "/nonexistent", "HOME": str(tmp_path), "LANG": "C"},
        capture_output=True, text=True, check=False, timeout=5)
    assert result.returncode == 127
    assert "BROKER_ENVIRONMENT_ERROR: MODULE_COMMAND_UNAVAILABLE" in result.stderr
    assert "No such file" not in result.stderr
