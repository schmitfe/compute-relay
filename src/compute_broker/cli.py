"""Trusted operator entry points. Never expose these commands as agent tools."""

import argparse
import json
import os
from pathlib import Path
import secrets


def setup():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "compute_broker.control.settings")
    import django
    django.setup()


def private_write(path, content):
    path = Path(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(content)


def bootstrap_demo(args):
    setup()
    from datetime import timedelta
    from django.core.management import call_command
    from django.db import transaction
    from django.utils import timezone
    from compute_broker.control.auth import new_token, token_hash
    from compute_broker.control.models import Connection, Environment, Grant, Project, ProjectCredential, User, WorkerIdentity
    from compute_broker.control.services import digest
    from compute_broker.domain.validation import validate

    call_command("migrate", interactive=False, verbosity=0)
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    if (output / "demo.json").exists():
        document = json.loads((output / "demo.json").read_text())
        if not Project.objects.filter(id=document["project_id"]).exists():
            raise SystemExit("Demo metadata belongs to a different database; choose a new output directory.")
        print(f"Existing synthetic demo configuration: {output / 'demo.json'}")
        return
    agent_token, worker_token = new_token(), new_token("cbw")
    password = secrets.token_urlsafe(24)
    with transaction.atomic():
        username = "demo-owner-" + secrets.token_hex(3)
        owner = User.objects.create_user(username=username, password=password)
        worker = WorkerIdentity.objects.create(owner=owner, name="Synthetic demo worker", token_hash=token_hash(worker_token))
        profile = {"partitions": ["debug"], "accounts": ["broker"], "qos": [], "maximum_cpus": 2,
                   "maximum_gpus": 0, "maximum_memory_mib": 1024, "maximum_time_limit_seconds": 600,
                   "termination_margin_seconds": 5, "overtime_margin_seconds": 0, "cleanup_margin_seconds": 30}
        config = {"backend": args.backend, "profile_revision": 1, "site_profile": profile}
        if args.backend == "fake":
            config["state_directory"] = args.fake_state_directory
        else:
            config.update({"remote_root": "/home/brokerjob/jobs", "python_path": "/usr/bin/python3",
                           "ssh": {"host": "slurm-fixture", "user": "brokerjob", "port": 22, "mode": "key",
                                   "identity_file": "/run/fixture-ssh/id_ed25519", "known_hosts": "/run/fixture-ssh/known_hosts"},
                           "slurm_paths": {name: "/usr/bin/" + name for name in ("sbatch", "squeue", "sacct", "scancel")}})
        # Credentials and executable paths belong only to the worker config.
        # The control database stores the sanitized public target/profile binding.
        from compute_broker.worker.enrollment import profile_metadata, resolve_connection
        alias = "demo-" + args.backend
        target = {"host": "slurm-fixture" if args.backend == "slurm" else "simulation", "user": "brokerjob", "port": 22}
        remote_root = "/home/brokerjob/jobs" if args.backend == "slurm" else "/simulated/jobs"
        protected = {key: value for key, value in config.items() if key not in {"remote_root", "profile_revision", "backend", "site_profile", "ssh"}}
        if "ssh" in config:
            protected["ssh"] = {key: value for key, value in config["ssh"].items() if key not in {"host", "user", "port"}}
        protected.update(backend=args.backend, profile_revision=1, site_profile=profile,
                         allowed_hosts=[target["host"]], allowed_users=[target["user"]], allowed_ports=[22], remote_root_prefixes=[remote_root])
        credential_profiles = {alias: protected}
        _, config, revision = resolve_connection({"backend": args.backend, "credential_profile": alias, "ssh": target, "remote_root": remote_root}, credential_profiles)
        worker.profiles = [profile_metadata(alias, protected)]
        worker.save(update_fields=["profiles"])
        connection = Connection.objects.create(owner=owner, worker=worker, name="Synthetic " + args.backend,
                                                backend=args.backend, configuration=config, revision_sha256=revision, discovery_state="queued")
        env = {"interpreter": "/usr/bin/python3", "modules": []}
        environment = Environment.objects.create(owner=owner, connection=connection, name="System Python (demo)", configuration=env, revision_sha256=digest(env))
        project = Project.objects.create(owner=owner, name="Synthetic demo")
        project.connections.add(connection)
        project.environments.add(environment)
        policy = {"schema_version": "1.0", "project_id": str(project.id), "revision": 1, "mode": args.mode,
                  "allowed_connection_ids": [str(connection.id)], "allowed_environment_revision_sha256": [environment.revision_sha256],
                  "allowed_gpu_types": [], "max_active_jobs": 2, "max_total_jobs": 1000,
                  "per_job_limits": {"cpu_cores": 2, "gpu_devices": 0, "memory_mib": 1024, "time_limit_seconds": 600, "maximum_charge_duration_seconds": 635},
                  "allowance": {"cpu_core_seconds": 7200, "gpu_device_seconds": 0},
                  "admission_expires_at": (timezone.now() + timedelta(days=7)).isoformat()}
        validate("project-policy.schema.json", policy)
        Grant.objects.create(project=project, policy=policy)
        credential = ProjectCredential(project=project, issued_by=owner, name="demo agent", token_hash=token_hash(agent_token),
                                        scopes=["objects:write", "jobs:read", "jobs:write", "jobs:execute", "jobs:cancel", "artifacts:read"], expires_at=timezone.now() + timedelta(days=7))
        credential.prefix = str(credential.id)[:12]
        credential.save()
        document = {"project_id": str(project.id), "connection_id": str(connection.id), "worker_id": str(worker.id), "backend": args.backend,
                    "environment_id": str(environment.id), "environment_revision_sha256": environment.revision_sha256,
                    "owner_username": username, "owner_password": password,
                    "cluster_form": {"worker": str(worker.id), "credential_profile": alias, "host": target["host"],
                                     "user": target["user"], "port": target["port"], "remote_root": remote_root}}
        private_write(output / "agent-token", agent_token + "\n")
        private_write(output / "worker-token", worker_token + "\n")
        private_write(output / "worker.json", json.dumps({"owner_id": str(owner.id), "connections": {}, "credential_profiles": credential_profiles}, indent=2) + "\n")
        private_write(output / "demo.json", json.dumps(document, indent=2) + "\n")
    print(f"Synthetic demo ready. Private owner/project metadata: {output / 'demo.json'}")
    print("This development demo does not isolate secrets from the desktop account.")


def discover(args):
    # This path deliberately never initializes Django or reads its credential DB.
    from compute_broker.backends import SlurmBackend
    if args.config:
        connection = json.loads(Path(args.config).read_text())
    else:
        user, separator, host = args.host.partition("@")
        if not separator:
            host, user = user, None
        ssh = {"host": host, "port": args.port, "mode": "key"}
        if user:
            ssh["user"] = user
        if args.identity_file:
            ssh["identity_file"] = args.identity_file
        if args.known_hosts:
            ssh["known_hosts"] = args.known_hosts
        connection = {"ssh": ssh, "python_path": "/usr/bin/python3"}
    result = SlurmBackend(connection).discover()
    content = json.dumps(result, indent=2) + "\n"
    if args.output:
        output = Path(args.output).resolve()
        output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        private_write(output, content)
        print(f"Read-only discovery saved to {output}")
    else:
        print(content)


def main():
    parser = argparse.ArgumentParser(description="Compute Broker development operator CLI")
    sub = parser.add_subparsers(dest="command", required=True)
    boot = sub.add_parser("bootstrap-demo", help="Create synthetic demo credentials and apply migrations")
    boot.add_argument("--output", required=True)
    boot.add_argument("--backend", choices=["fake", "slurm"], default="fake")
    boot.add_argument("--mode", choices=["approval_required", "budgeted", "mixed"], default="approval_required")
    boot.add_argument("--fake-state-directory", default="/worker-state/fake")
    boot.set_defaults(run=bootstrap_demo)
    disc = sub.add_parser("discover", help="Read scheduler capabilities over SSH; never submit jobs")
    selector = disc.add_mutually_exclusive_group(required=True)
    selector.add_argument("--host", help="SSH host or user@host; uses ordinary strict OpenSSH authentication")
    selector.add_argument("--config", help="Trusted connection JSON file")
    disc.add_argument("--port", type=int, default=22)
    disc.add_argument("--identity-file")
    disc.add_argument("--known-hosts")
    disc.add_argument("--output", help="New private JSON file for the discovery snapshot")
    disc.set_defaults(run=discover)
    args = parser.parse_args()
    args.run(args)


if __name__ == "__main__":
    main()
