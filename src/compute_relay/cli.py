"""Local client setup and trusted operator commands; not an agent tool surface."""

import argparse
import json
import os
from pathlib import Path
import secrets


def setup():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "compute_relay.control.settings")
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
    from compute_relay.control.auth import new_token, token_hash
    from compute_relay.control.models import Connection, Environment, Grant, Project, ProjectCredential, User, WorkerIdentity
    from compute_relay.control.services import digest
    from compute_relay.domain.validation import validate

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
                   "automatic_execution_allowed": True}
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
        from compute_relay.worker.enrollment import profile_metadata, resolve_connection
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
                  "per_job_limits": {"cpu_cores": 2, "gpu_devices": 0, "memory_mib": 1024, "time_limit_seconds": 600},
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
    import asyncio
    # This path deliberately never initializes Django or reads its credential DB.
    from compute_relay.backends import SlurmBackend
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
    result = asyncio.run(SlurmBackend(connection).discover())
    content = json.dumps(result, indent=2) + "\n"
    if args.output:
        output = Path(args.output).resolve()
        output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        private_write(output, content)
        print(f"Read-only discovery saved to {output}")
    else:
        print(content)


def main(argv=None):
    from .local_runtime import register_options, run as run_local
    parser = argparse.ArgumentParser(description="Compute Relay: local compute access for AI agents")
    register_options(parser)
    parser.set_defaults(run=run_local)
    sub = parser.add_subparsers(dest="command")
    start = sub.add_parser("start", help="Start the local portal, SQLite state and saved cluster connectors")
    register_options(start)
    start.set_defaults(run=run_local)
    from .local_maintenance import run as maintain_state
    maintenance = sub.add_parser("maintain-state", help="Inspect or compact bounded local metadata while the application is stopped")
    maintenance.add_argument("--data-directory", default=argparse.SUPPRESS, help="Existing private installation state directory")
    maintenance.add_argument("--archive-drafts", action="store_true", help="Select old, unreferenced jobs with no execution request")
    maintenance.add_argument("--compact-idempotency", action="store_true", help="Retire old response bodies, keeping non-replayable request identities")
    maintenance.add_argument("--older-than-days", type=int, default=30)
    maintenance.add_argument("--limit", type=int, default=100, help="Maximum records per maintenance category (1 through 1000)")
    maintenance.add_argument("--apply", action="store_true", help="Apply the selected compaction and recover expired uploads; otherwise inspect only")
    maintenance.set_defaults(run=maintain_state)
    from compute_relay.mcp.setup import register_parser
    from compute_relay.connector_profiles import register_commands
    register_parser(sub)
    register_commands(sub)
    from compute_relay.workspace import register_commands as register_file_commands
    register_file_commands(sub)
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
    from compute_relay.worker.personal_connector import main as connector_main
    connector = sub.add_parser("connector", help="Start your personal cluster connector and complete SSH/MFA login locally")
    connector.add_argument("--setup", help="Private one-time setup JSON downloaded from your cluster page")
    connector.add_argument("--state-directory", required=True, help="Private 0700 local directory; keep the socket path under 100 bytes")
    connector.add_argument("--registry-directory", help="Private registry in which to remember this paired connector")
    connector.add_argument("--identity-file", help="Explicit local key path override for first pairing")
    connector.add_argument("--choose-identity", action="store_true", help="Browse for your local SSH key in a file-picker window")
    connector.add_argument("--known-hosts", help="Local known-hosts file override for first pairing")
    connector.add_argument("--non-interactive", action="store_true", help="Fail if SSH requires a prompt")
    connector.add_argument("--allow-insecure-http", action="store_true", help="Allow an explicitly selected isolated HTTP test network")
    connector.add_argument("--verbose", action="store_true", help="Show sanitized connector activity without credentials or job contents")
    connector.add_argument("--once", action="store_true", help=argparse.SUPPRESS)
    connector.set_defaults(run=connector_main, register_connector=True)
    args = parser.parse_args(argv)
    args.run(args)


if __name__ == "__main__":
    main()
