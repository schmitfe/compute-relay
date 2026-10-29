#!/usr/bin/env python3
"""Build distributions, install a wheel into a clean venv, and start it locally.

Uses disposable state and loopback only. Requires uv, the test extra for `build` and
network/package-cache access for build and runtime dependencies. No Docker,
SSH credential, existing installation, project key, or cluster is used.
"""
from __future__ import annotations

import argparse
from email.parser import BytesParser
import json
import os
from pathlib import Path
import signal
import socket
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import venv
import zipfile
import shutil

ROOT = Path(__file__).resolve().parents[1]


def run(argv, *, cwd, env, timeout=180):
    result = subprocess.run([str(item) for item in argv], cwd=cwd, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"Package check failed ({result.returncode}): {argv[0]}\n{result.stdout[-12000:]}")
    return result.stdout


def expected_package_files(source):
    package = source / "src/compute_relay"
    if not (package / "__init__.py").is_file() or not (package / "local_runtime.py").is_file():
        raise RuntimeError("Canonical compute_relay package or local launcher source is missing")
    return {"compute_relay/" + path.relative_to(package).as_posix()
        for path in package.rglob("*") if path.is_file() and
        (path.suffix in {".py", ".html"} or
         (path.suffix == ".json" and "schemas" in path.parts) or
         (path.suffix == ".md" and "resources" in path.parts))}


def inspect_distributions(source, distribution):
    wheels = list(distribution.glob("*.whl"));sdists = list(distribution.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise RuntimeError("Expected exactly one wheel and source distribution")
    expected = expected_package_files(source)
    with zipfile.ZipFile(wheels[0]) as wheel:
        metadata_files = [name for name in wheel.namelist() if name.endswith(".dist-info/METADATA")]
        if len(metadata_files) != 1:
            raise RuntimeError("Expected exactly one wheel metadata file")
        metadata = BytesParser().parsebytes(wheel.read(metadata_files[0]))
        license_path = metadata_files[0].removesuffix("METADATA") + "licenses/LICENSE"
        if (metadata.get("License-Expression") != "Apache-2.0"
                or "LICENSE" not in metadata.get_all("License-File", [])
                or license_path not in wheel.namelist()
                or wheel.read(license_path) != (source / "LICENSE").read_bytes()):
            raise RuntimeError("Wheel omitted Apache-2.0 metadata or license text")
        missing = expected - set(wheel.namelist())
        if missing:
            raise RuntimeError("Wheel omitted runtime files: " + ", ".join(sorted(missing)))
        forbidden = [name for name in wheel.namelist() if name.startswith(("local/", ".git/", "deploy/ldap/"))]
        if forbidden:
            raise RuntimeError("Wheel contains non-package private/server paths")
    with tarfile.open(sdists[0]) as archive:
        members = {"/".join(item.name.split("/")[1:]) for item in archive.getmembers()}
        missing = {"src/" + name for name in expected} - members
        if missing or not {"pyproject.toml", "README.md", "LICENSE"}.issubset(members):
            raise RuntimeError("Source distribution is incomplete")
    return wheels[0], len(expected)


def wait_http(process, url, log):
    deadline = time.monotonic() + 60
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("Installed launcher exited before readiness: " + log.read_text(errors="replace")[-8000:])
        try:
            with opener.open(url + "/healthz", timeout=1) as response:
                if response.status == 200:
                    return opener
        except (OSError, urllib.error.URLError):
            time.sleep(0.1)
    raise RuntimeError("Installed launcher did not become ready")


def installed_launcher(python, root, env):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0));port = listener.getsockname()[1]
    state = root / "state"
    executable = python.parent / ("compute-relay.exe" if os.name == "nt" else "compute-relay")
    argv = [str(executable), "--data-directory", str(state), "--port", str(port),
        "--no-browser", "--no-connectors", "--non-interactive", "--no-password"]
    log = root / "launcher.log"
    saved_owners = None
    for generation in range(2):
        with log.open("w") as output:
            process = subprocess.Popen(argv, cwd=root, env=env, stdout=output, stderr=subprocess.STDOUT)
        try:
            opener = wait_http(process, f"http://127.0.0.1:{port}", log)
            with opener.open(f"http://127.0.0.1:{port}/", timeout=5) as response:
                document = response.read(2 * 1024 * 1024).decode()
                if response.status != 200 or "<html" not in document.lower():
                    raise RuntimeError("Installed portal did not render its HTML templates")
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGINT)
                try:
                    process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    process.kill();process.wait()
                    raise RuntimeError("Installed launcher did not stop after Ctrl-C")
        if process.returncode not in (0, 130, -signal.SIGINT):
            raise RuntimeError("Installed launcher shutdown failed: " + log.read_text(errors="replace")[-8000:])
        databases = list(state.rglob("*.sqlite3"))
        if not databases:
            raise RuntimeError("Installed launcher did not create its durable SQLite state")
        with sqlite3.connect(f"file:{databases[0]}?mode=ro", uri=True) as database:
            owners = database.execute("SELECT id,username FROM broker_user ORDER BY id").fetchall()
        if len(owners) != 1 or owners[0][1] != "local":
            raise RuntimeError("Installed launcher did not retain one local owner")
        if saved_owners is not None and owners != saved_owners:
            raise RuntimeError("Restart replaced the saved local owner")
        saved_owners = owners
    return "start, HTML rendering, Ctrl-C, and same-state restart"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-launch", action="store_true", help="Only build/install/import; launcher acceptance remains pending")
    args = parser.parse_args()
    clean_env = {key: value for key, value in os.environ.items() if not key.startswith("BROKER_") and
        key not in {"PYTHONPATH", "DJANGO_SETTINGS_MODULE", "VIRTUAL_ENV"}}
    clean_env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    with tempfile.TemporaryDirectory(prefix="compute-relay-package-") as temporary:
        root = Path(temporary);source = root / "source";source.mkdir()
        for name in ("pyproject.toml", "README.md", "LICENSE"):
            shutil.copy2(ROOT / name, source / name)
        shutil.copytree(ROOT / "src", source / "src", ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.egg-info"))
        distribution = root / "dist"
        run([sys.executable, "-m", "build", "--sdist", "--wheel", "--outdir", distribution, source], cwd=root, env=clean_env)
        wheel, count = inspect_distributions(source, distribution)
        constraints = root / "runtime-requirements.txt"
        run(["uv", "export", "--locked", "--no-dev", "--no-emit-project", "--no-hashes",
             "--output-file", constraints], cwd=ROOT, env=clean_env)
        target = root / "venv";venv.EnvBuilder(with_pip=True).create(target)
        python = target / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        run([python, "-m", "pip", "install", "--constraint", constraints, wheel], cwd=root, env=clean_env)
        run([python, "-m", "pip", "check"], cwd=root, env=clean_env)
        run([python, "-c", "from importlib import import_module\n"
             "from compute_relay.remote.bundles import bundle_source,component_names\n"
             "from compute_relay.backends.preparation import helper_source\n"
             "assert helper_source() == bundle_source('jobs')\n"
             "assert set(component_names('jobs')).isdisjoint({'software.py','module_catalog.py','discovery.py'})\n"
             "for kind in ('jobs','discovery'):\n"
             "    source = bundle_source(kind)\n"
             "    assert source == bundle_source(kind)\n"
             "    compile(source, kind, 'exec')\n"
             "    for component in component_names(kind):\n"
             "        import_module('compute_relay.remote.' + component[:-3])\n"] , cwd=root, env=clean_env)
        run([python, "-c", "import django,httpx,mcp,waitress,jsonschema,rfc8785; from compute_relay.sdk import WorkspaceClient,ProjectFacade,finish; from importlib.metadata import distribution; package=distribution('compute-relay'); assert not any('extra ==' in requirement for requirement in package.requires or [] if requirement.startswith(('httpx','mcp','waitress'))); assert {entry.name for entry in package.entry_points if entry.group == 'console_scripts'} == {'compute-relay'}"] , cwd=root, env=clean_env)
        executable = python.parent / ("compute-relay.exe" if os.name == "nt" else "compute-relay")
        run([executable, "--help"], cwd=root, env=clean_env)
        result = "not requested (--skip-launch)" if args.skip_launch else installed_launcher(python, root, clean_env)
        print(json.dumps({"wheel": "passed", "sdist": "passed", "runtime_files": count,
            "clean_install": "passed", "dependency_check": "passed", "launcher": result}, sort_keys=True))


if __name__ == "__main__":
    main()
