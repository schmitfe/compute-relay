"""Public login identity without credential material or filesystem settings."""

import re


def public_ssh_identity(configuration):
    if not isinstance(configuration, dict) or not isinstance(configuration.get("ssh"), dict):
        return None
    ssh = configuration["ssh"]
    host, user, port = ssh.get("host"), ssh.get("user"), ssh.get("port", 22)
    if (not isinstance(host, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,253}", host)
            or host.startswith("-") or not isinstance(user, str)
            or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,79}", user)
            or type(port) is not int or not 1 <= port <= 65535):
        return None
    return {"host": host, "user": user, "port": port}
