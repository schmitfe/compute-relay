"""Inspect deployment intent without claiming a running container boundary.

These tests need no container daemon, package downloads, SSH identity, or cluster.
The Compose documents use the JSON subset of YAML so the standard library suffices.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def compose(name):
    return json.loads((ROOT / "deploy" / name).read_text(encoding="utf-8"))


def mount_sources(service):
    return {mount.split(":", 1)[0] for mount in service.get("volumes", [])}


class ContainerLayoutTests(unittest.TestCase):
    def test_networks_and_published_ports_are_local_fixture_only(self):
        for name in ("compose.test.yml", "compose.fake.yml"):
            with self.subTest(compose=name):
                configuration = compose(name)
                self.assertTrue(configuration["networks"]["fixture"]["internal"])
                for role, service in configuration["services"].items():
                    expected = ["127.0.0.1:${BROKER_TEST_PORT:-8765}:8765"] if role == "control" else []
                    self.assertEqual(service.get("ports", []), expected)
                    self.assertNotEqual(service.get("network_mode"), "host")
                    self.assertFalse(service.get("privileged", False))

    def test_worker_has_no_control_database_or_owner_metadata_mount(self):
        for name in ("compose.test.yml", "compose.fake.yml"):
            with self.subTest(compose=name):
                services = compose(name)["services"]
                worker_sources = mount_sources(services["worker"])
                self.assertNotIn("control-data", worker_sources)
                self.assertNotIn("demo-metadata", worker_sources)
                self.assertIn("worker-config", worker_sources)
                self.assertNotIn("fixture-ssh", mount_sources(services["control"]))
                self.assertNotIn("fixture-ssh", mount_sources(services["smoke"]))
                self.assertNotIn("--management", services["smoke"]["command"])
                self.assertEqual(
                    services["worker"]["environment"]["BROKER_WORKER_CONFIG"],
                    "/run/demo/worker.json",
                )

    def test_application_services_drop_caps_and_do_not_mount_host_sockets(self):
        for name in ("compose.test.yml", "compose.fake.yml"):
            for role, service in compose(name)["services"].items():
                with self.subTest(compose=name, service=role):
                    for mount in service.get("volumes", []):
                        self.assertNotIn("docker.sock", mount)
                        self.assertNotIn("podman.sock", mount)
                        self.assertFalse(mount.startswith(("/", "~", ".")))
                    if role != "slurm":
                        self.assertTrue(service["read_only"])
                        self.assertIn("ALL", service["cap_drop"])
                        self.assertIn("no-new-privileges:true", service["security_opt"])
                    for variable in service.get("environment", {}):
                        # The worker has a token-file path, never an inline secret.
                        self.assertNotIn(variable, {"BROKER_WORKER_TOKEN", "SSH_AUTH_SOCK"})

    def test_fake_stack_cannot_accidentally_start_the_real_fixture(self):
        services = compose("compose.fake.yml")["services"]
        self.assertNotIn("slurm", services)
        self.assertEqual(services["control"]["environment"]["BROKER_DEMO_BACKEND"], "fake")
        self.assertNotIn("fixture-ssh", mount_sources(services["worker"]))

    def test_slurm_fixture_installs_real_accounting_packages(self):
        source = (ROOT / "deploy/slurm/Dockerfile").read_text()
        for package in ("slurm-wlm", "slurmdbd", "slurm-wlm-mysql-plugin", "munge", "mariadb-server"):
            self.assertIn(package, source)
        self.assertIn("FROM debian:bookworm-slim", source)
        config = (ROOT / "deploy/slurm/slurm.conf").read_text()
        for required in (
            "AccountingStorageType=accounting_storage/slurmdbd",
            "AccountingStorageEnforce=associations",
            "JobRequeue=0", "KillWait=5", "OverTimeLimit=0",
            "TaskPlugin=task/none", "ProctrackType=proctrack/linuxproc",
        ):
            self.assertIn(required, config)

    def test_fixture_credentials_are_created_at_runtime(self):
        entrypoint = (ROOT / "deploy/slurm/entrypoint.sh").read_text()
        self.assertIn("ssh-keygen", entrypoint)
        self.assertIn("/dev/urandom", entrypoint)
        self.assertIn("secrets.token_hex", entrypoint)
        for path in (ROOT / "deploy").rglob("*"):
            if path.is_file():
                self.assertNotIn(b"BEGIN OPENSSH PRIVATE KEY", path.read_bytes(), str(path))
        config = (ROOT / "deploy/slurm/sshd_config").read_text()
        self.assertIn("PasswordAuthentication no", config)
        self.assertIn("PermitRootLogin no", config)

    def test_build_contexts_are_allowlisted(self):
        app = (ROOT / "deploy/Dockerfile.dockerignore").read_text().splitlines()
        fixture = (ROOT / "deploy/slurm/Dockerfile.dockerignore").read_text().splitlines()
        self.assertEqual(app[0], "**")
        self.assertEqual(fixture[0], "**")
        self.assertIn("!tools/container_smoke.py", app)
        for source in (app, fixture):
            for pattern in source:
                self.assertNotIn(pattern, {"!.git/**", "!.ssh/**", "!.env", "!data/**", "!**"})

    @unittest.skipUnless(shutil.which("bash"), "bash is required for shell syntax validation")
    def test_shell_syntax(self):
        for path in (ROOT / "deploy").rglob("*.sh"):
            with self.subTest(script=str(path.relative_to(ROOT))):
                result = subprocess.run(
                    ["bash", "-n", str(path)], capture_output=True, text=True, timeout=5,
                )
                self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
