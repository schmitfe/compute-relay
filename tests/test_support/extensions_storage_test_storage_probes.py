import pytest
from django.utils import timezone
from compute_relay.extensions.storage import services
from compute_relay.worker.enrollment import profile_metadata, resolve_connection

@pytest.fixture
def probes(resources, tmp_path):
    root = tmp_path / "enrolled"
    root.mkdir()
    profile = {"backend": "slurm", "allowed_hosts": ["storage.invalid"], "allowed_users": ["researcher"], "allowed_ports": [22],
               "remote_root_prefixes": [str(root)], "ssh": {"identity_file": "/private/not-readable-by-agent",
                                                            "known_hosts": "/private/pinned-hosts"}}
    public = {"backend": "slurm", "credential_profile": "protected", "ssh": {"host": "storage.invalid", "user": "researcher", "port": 22},
              "remote_root": str(root) + "/jobs"}
    private, public, revision = resolve_connection(public, {"protected": profile})
    resources.worker.profiles = [profile_metadata("protected", profile)]
    resources.worker.supported_extensions = ["storage"]
    resources.worker.save(update_fields=["profiles", "supported_extensions"])
    resources.connection.configuration, resources.connection.revision_sha256 = public, revision
    resources.connection.backend = "slurm"
    resources.connection.save(update_fields=["configuration", "revision_sha256", "backend"])
    target = services.register_target(resources.owner, resources.connection, "staging-root-0", "Test metadata only",
                                     project=resources.project)
    resources.target, resources.profile, resources.private, resources.public, resources.root = target, profile, private, public, root
    return resources

def lease(probes):
    services.set_probe_enabled(probes.owner, probes.target.pool, True)
    document = services.lease_work(probes.worker)
    assert document is not None
    return {**document, "operation": "extension", "extension": "storage"}

def sample():
    return {"schema_version": "1.0", "source": "statvfs", "status": "available", "observed_at": timezone.now().isoformat(),
            "filesystem_id": "1:2", "identity_verified": False, "total_bytes": 10000000000, "available_bytes": 8000000000,
            "total_inodes": 10000, "available_inodes": 8000, "quota": {"status": "unknown"}}
