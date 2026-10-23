"""Resolve public connection requests against protected credential profiles.

Only public target metadata crosses the control API. SSH credentials and executable
paths remain in this worker's administrator-owned configuration.
"""

from __future__ import annotations

import copy
import hashlib
import posixpath
import re

from compute_broker.backends import canonical_json_bytes
from compute_broker.domain.cluster_configuration import TIMING_FIELDS


class EnrollmentError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


_ALIAS = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,79}\Z")
_HOST = re.compile(r"[A-Za-z0-9_.:-]{1,253}\Z")
_USER = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,79}\Z")
_ROOT = re.compile(r"/[A-Za-z0-9_./-]+\Z")
_SITE_FIELDS = {
    "automatic_execution_allowed",
    "partitions", "accounts", "qos", "termination_margin_seconds",
    "overtime_margin_seconds", "cleanup_margin_seconds", "allocated_cpus",
    "allocated_gpus", "maximum_cpus", "maximum_gpus", "maximum_memory_mib",
    "maximum_time_limit_seconds",
}
_RUNTIME_FIELDS = {
    "ssh", "ssh_path", "python_path", "slurm_paths", "scheduler_environment",
    "command_timeout_seconds", "state_directory", "fake_outcome", "fake_elapsed_seconds",
    "environment_discovery",
}
_PROFILE_FIELDS = _RUNTIME_FIELDS | {
    "backend", "allowed_hosts", "allowed_users", "allowed_ports",
    "remote_root_prefixes", "profile_revision", "site_profile",
}
_PUBLIC_FIELDS = {
    "backend", "credential_profile", "ssh", "remote_root", "profile_revision",
    "site_profile", "credential_profile_revision_sha256", "revision_sha256",
}


def _root(value):
    if (not isinstance(value, str) or not _ROOT.fullmatch(value)
            or value == "/" or value.startswith("//") or posixpath.normpath(value) != value
            or any(part in {".", ".."} for part in value.split("/"))):
        raise EnrollmentError("INVALID_CONNECTION_CONFIGURATION")
    return value


def _listed(profile, key, validator):
    values = profile.get(key)
    if not isinstance(values, list) or not 1 <= len(values) <= 100:
        raise EnrollmentError("INVALID_CREDENTIAL_PROFILE")
    for value in values:
        if not validator(value):
            raise EnrollmentError("INVALID_CREDENTIAL_PROFILE")
    return values


def profile_metadata(alias: str, profile: dict) -> dict:
    if not isinstance(alias, str) or not _ALIAS.fullmatch(alias) or not isinstance(profile, dict):
        raise EnrollmentError("INVALID_CREDENTIAL_PROFILE")
    if set(profile) - _PROFILE_FIELDS or profile.get("backend") not in {"fake", "slurm"}:
        raise EnrollmentError("INVALID_CREDENTIAL_PROFILE")
    hosts = _listed(profile, "allowed_hosts", lambda v: isinstance(v, str) and bool(_HOST.fullmatch(v)) and not v.startswith("-"))
    users = _listed(profile, "allowed_users", lambda v: isinstance(v, str) and bool(_USER.fullmatch(v)))
    ports = _listed(profile, "allowed_ports", lambda v: type(v) is int and 1 <= v <= 65535)
    roots = _listed(profile, "remote_root_prefixes", lambda v: bool(_root(v)))
    revision = profile.get("profile_revision", 1)
    if type(revision) is not int or not 1 <= revision <= 2**53 - 1:
        raise EnrollmentError("INVALID_CREDENTIAL_PROFILE")
    site = profile.get("site_profile", {})
    if not isinstance(site, dict) or set(site) - _SITE_FIELDS:
        raise EnrollmentError("INVALID_CREDENTIAL_PROFILE")
    for key, value in site.items():
        if key in TIMING_FIELDS:
            # Retained raw private state is still fingerprinted for old jobs,
            # but these obsolete settings do not enter active public metadata.
            continue
        if key == "automatic_execution_allowed":
            if type(value) is not bool:
                raise EnrollmentError("INVALID_CREDENTIAL_PROFILE")
        elif key in {"partitions", "accounts", "qos"}:
            if (not isinstance(value, list) or len(value) > 100
                    or any(not isinstance(item, str) or not _ALIAS.fullmatch(item) for item in value)):
                raise EnrollmentError("INVALID_CREDENTIAL_PROFILE")
        elif type(value) is not int or not 0 <= value <= 2**53 - 1:
            raise EnrollmentError("INVALID_CREDENTIAL_PROFILE")
    try:
        fingerprint = hashlib.sha256(canonical_json_bytes(profile)).hexdigest()
    except ValueError:
        raise EnrollmentError("INVALID_CREDENTIAL_PROFILE") from None
    return {"alias": alias, "backend": profile["backend"], "revision_sha256": fingerprint,
            "allowed_hosts": copy.deepcopy(hosts), "allowed_users": copy.deepcopy(users),
            "allowed_ports": copy.deepcopy(ports), "remote_root_prefixes": copy.deepcopy(roots),
            "site_profile": {key: copy.deepcopy(value) for key, value in site.items() if key not in TIMING_FIELDS}}


def resolve_connection(configuration: dict, profiles: dict, *, refresh: bool = False) -> tuple[dict, dict, str]:
    """Return (private runtime config, safe public config, public revision digest)."""
    if not isinstance(configuration, dict) or set(configuration) - _PUBLIC_FIELDS:
        raise EnrollmentError("INVALID_CONNECTION_CONFIGURATION")
    alias = configuration.get("credential_profile")
    if not isinstance(alias, str) or alias not in profiles:
        raise EnrollmentError("CREDENTIAL_PROFILE_UNKNOWN")
    profile = profiles[alias]
    metadata = profile_metadata(alias, profile)
    ssh = configuration.get("ssh")
    if not isinstance(ssh, dict) or set(ssh) != {"host", "user", "port"}:
        raise EnrollmentError("INVALID_CONNECTION_CONFIGURATION")
    root = _root(configuration.get("remote_root"))
    if (configuration.get("backend") != metadata["backend"]
            or ssh["host"] not in metadata["allowed_hosts"]
            or ssh["user"] not in metadata["allowed_users"]
            or type(ssh["port"]) is not int or ssh["port"] not in metadata["allowed_ports"]
            or not any(root == prefix or root.startswith(prefix + "/")
                       for prefix in metadata["remote_root_prefixes"])):
        raise EnrollmentError("TARGET_NOT_ALLOWED")
    revision = profile.get("profile_revision", 1)
    fingerprint = metadata["revision_sha256"]
    configured_site = configuration.get("site_profile", metadata["site_profile"])
    if not isinstance(configured_site, dict):
        raise EnrollmentError("INVALID_CONNECTION_CONFIGURATION")
    active_site = {key: value for key, value in configured_site.items() if key not in TIMING_FIELDS}
    if not refresh and (configuration.get("profile_revision", revision) != revision
            or configuration.get("credential_profile_revision_sha256", fingerprint) != fingerprint
            or active_site != metadata["site_profile"]):
        raise EnrollmentError("CREDENTIAL_PROFILE_CHANGED")
    public = {"backend": metadata["backend"], "credential_profile": alias, "ssh": copy.deepcopy(ssh),
              "remote_root": root, "profile_revision": revision,
              "credential_profile_revision_sha256": fingerprint,
              "site_profile": metadata["site_profile"]}
    if not refresh and any(key in configured_site for key in TIMING_FIELDS):
        # Keep the immutable digest used by an already prepared historical
        # package. A normal discovery refresh publishes the canonical profile.
        public["site_profile"] = copy.deepcopy(configured_site)
    digest = hashlib.sha256(canonical_json_bytes(public)).hexdigest()
    if not refresh and configuration.get("revision_sha256", digest) != digest:
        raise EnrollmentError("CONNECTION_REVISION_CHANGED")
    private = {key: copy.deepcopy(profile[key]) for key in _RUNTIME_FIELDS if key in profile}
    private.update(backend=metadata["backend"], remote_root=root, profile_revision=revision,
                   site_profile=copy.deepcopy(metadata["site_profile"]), revision_sha256=digest)
    private["ssh"] = {**private.get("ssh", {}), **copy.deepcopy(ssh)}
    return private, public, digest
