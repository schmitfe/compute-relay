"""Private, durable installation metadata and a single local launcher lock."""

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import secrets
from uuid import UUID

from .connector_profiles import ConnectorProfileError, _directory, _path, _private_file
from .domain.validation import strict_json
from .extensions import configured_names


class LocalStateError(ValueError):
    pass


class AlreadyRunning(LocalStateError):
    pass


def default_directory():
    configured = os.environ.get("XDG_DATA_HOME", "")
    base = Path(configured) if os.path.isabs(configured) else Path.home() / ".local/share"
    return base / "compute-relay"


class LocalState:
    def __init__(self, directory):
        self.directory = _path(directory)
        self.descriptor = None

    def __enter__(self):
        try:
            self.descriptor = _directory(self.directory, create=True)
        except (OSError, ConnectorProfileError) as error:
            raise LocalStateError("Choose a private state directory owned by you (0700), without symbolic links.") from error
        return self

    def __exit__(self, *args):
        os.close(self.descriptor)
        self.descriptor = None

    def read(self, name):
        try:
            descriptor = _private_file(self.descriptor, name, os.O_RDONLY)
        except FileNotFoundError:
            return None
        except (OSError, ConnectorProfileError):
            raise LocalStateError("Installation metadata must be private regular files owned by you, without links.") from None
        with os.fdopen(descriptor, "rb") as stream:
            raw = stream.read(65537)
        try:
            if len(raw) > 65536:
                raise ValueError
            result = strict_json(raw)
            if not isinstance(result, dict):
                raise ValueError
            return result
        except (ValueError, UnicodeError):
            raise LocalStateError("Installation metadata is invalid; restore it or choose a new state directory.") from None

    def write(self, name, document):
        # Validate any existing destination before replacing it, including links.
        self.read(name)
        raw = json.dumps(document, indent=2).encode("utf-8") + b"\n"
        if len(raw) > 65536:
            raise LocalStateError("Installation metadata exceeds its size limit.")
        temporary = ".installation-" + secrets.token_hex(16)
        descriptor = _private_file(self.descriptor, temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(raw);stream.flush();os.fsync(stream.fileno())
            os.replace(temporary, name, src_dir_fd=self.descriptor, dst_dir_fd=self.descriptor)
            os.fsync(self.descriptor)
        finally:
            try:
                os.unlink(temporary, dir_fd=self.descriptor)
            except FileNotFoundError:
                pass

    @contextmanager
    def lock(self):
        try:
            descriptor = _private_file(self.descriptor, "runtime.lock", os.O_RDWR | os.O_CREAT)
        except (OSError, ConnectorProfileError):
            raise LocalStateError("Cannot safely open the installation lock.") from None
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise AlreadyRunning("This installation is already starting or running in another terminal.") from None
            yield
        finally:
            os.close(descriptor)

    def log(self, name):
        try:
            descriptor = _private_file(self.descriptor, name, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
            return os.fdopen(descriptor, "ab", buffering=0)
        except (OSError, ConnectorProfileError):
            raise LocalStateError("Cannot safely open the private service log.") from None


def validate_installation(document):
    expected = {"schema_version", "installation_id", "owner_id", "portal_auth", "port", "extensions", "initialized"}
    try:
        if not isinstance(document, dict) or set(document) != expected or document["schema_version"] != "1.0":
            raise ValueError
        for field in ("installation_id", "owner_id"):
            if str(UUID(document[field])) != document[field]:
                raise ValueError
        if document["portal_auth"] not in {"password", "none"} or type(document["initialized"]) is not bool:
            raise ValueError
        if type(document["port"]) is not int or not 1 <= document["port"] <= 65535:
            raise ValueError
        if not isinstance(document["extensions"], list) or list(configured_names(",".join(document["extensions"]))) != document["extensions"]:
            raise ValueError
    except (ValueError, TypeError, KeyError, AttributeError):
        raise LocalStateError("Installation metadata is invalid; restore it before starting.") from None
    return dict(document)
