"""Short-lived, single-use portal access tied to private local launch state."""

import fcntl
import hashlib
import hmac
import json
import os
import secrets
import time

from .connector_profiles import _private_file
from .local_state import LocalState, LocalStateError


def issue(state, origin, instance_id):
    token = secrets.token_urlsafe(32)
    state.write("portal-token.json", {"sha256": hashlib.sha256(token.encode()).hexdigest(),
        "expires_at": time.time() + 300, "instance_id": str(instance_id)})
    return origin + "/login/?launch_token=" + token


def consume(directory, token, instance_id):
    if not isinstance(token, str) or not 32 <= len(token) <= 128 or not instance_id:
        return False
    try:
        with LocalState(directory, create=False) as state:
            fd = _private_file(state.descriptor, "portal-token.json", os.O_RDWR)
            with os.fdopen(fd, "r+b") as stream:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
                document = json.loads(stream.read(4097))
                if (not isinstance(document, dict) or document.get("instance_id") != str(instance_id)
                        or not isinstance(document.get("expires_at"), (int, float))
                        or document["expires_at"] <= time.time()
                        or not isinstance(document.get("sha256"), str)
                        or not hmac.compare_digest(document["sha256"], hashlib.sha256(token.encode()).hexdigest())):
                    return False
                stream.seek(0)
                stream.write(b"{}\n")
                stream.truncate()
                stream.flush()
                os.fsync(stream.fileno())
                return True
    except (OSError, ValueError, LocalStateError):
        return False
