"""Versioned finite frames on SSH stdio; no listener or project-code imports."""

import json
import os
import re
import resource
import struct
import sys

from .prelude import MAX_REQUEST

STREAM_PROTOCOL = "compute-relay-stream/v1"
MAX_RESPONSE = 20 * 1024 * 1024


def stream_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate helper frame key")
        value[key] = item
    return value


def stream_number(_):
    raise ValueError("Nonfinite helper frame value")


def stream_read(size):
    content = bytearray()
    while len(content) < size:
        chunk = sys.stdin.buffer.read(size - len(content))
        if not chunk:
            if not content:
                return None
            raise ValueError("Truncated helper frame")
        content.extend(chunk)
    return bytes(content)


def stream_main(execute):
    os.umask(0o077)
    bound_config = None
    while True:
        header = stream_read(4)
        if header is None:
            return
        size = struct.unpack("!I", header)[0]
        if not 1 <= size <= MAX_REQUEST:
            raise ValueError("Helper frame exceeds request bound")
        payload = stream_read(size)
        if payload is None:
            raise ValueError("Missing helper payload")
        envelope = json.loads(payload, object_pairs_hook=stream_object, parse_constant=stream_number)
        if not isinstance(envelope, dict) or set(envelope) != {"protocol", "id", "request"}:
            raise ValueError("Invalid helper frame")
        identifier = envelope.get("id")
        if (envelope.get("protocol") != STREAM_PROTOCOL or not isinstance(identifier, str)
                or not re.fullmatch(r"[0-9a-f]{32}", identifier)):
            raise ValueError("Invalid helper frame identity")
        response = {"protocol": STREAM_PROTOCOL, "id": identifier}
        try:
            request = envelope["request"]
            configuration = json.dumps(request["config"], sort_keys=True, separators=(",", ":"), allow_nan=False)
            if bound_config is None:
                bound_config = configuration
            if configuration != bound_config:
                raise ValueError("Persistent helper configuration changed")
            response.update(ok=True, result=execute(request["config"], request))
        except Exception:
            response.update(ok=False, error="REMOTE_OPERATION_FAILED")
        response["max_rss_kib"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        encoded = json.dumps(response, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode()
        if len(encoded) > MAX_RESPONSE:
            raise ValueError("Helper frame exceeds response bound")
        sys.stdout.buffer.write(struct.pack("!I", len(encoded)))
        sys.stdout.buffer.write(encoded)
        sys.stdout.buffer.flush()
        request = envelope = payload = response = encoded = None
