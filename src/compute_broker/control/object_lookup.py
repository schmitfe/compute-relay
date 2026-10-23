"""Bounded, project-owned content lookup for workspace source staging."""

import re

from django.conf import settings

from .errors import BrokerError
from .models import Blob
from .storage import read_blob
from .views import body, boundary, scope


@boundary("POST")
def lookup(request, project_id):
    _, project = scope(request, "objects:write", project_id=project_id)
    document = body(request, maximum=32 * 1024)
    entries = document.get("objects")
    if set(document) != {"objects"} or not isinstance(entries, list) or not 1 <= len(entries) <= 100:
        raise BrokerError("INVALID_REQUEST", "List between one and 100 content hashes.")
    descriptors, size = [], 0
    for entry in entries:
        if (not isinstance(entry, dict) or set(entry) != {"sha256", "size_bytes"}
                or not isinstance(entry["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])
                or type(entry["size_bytes"]) is not int or not 0 <= entry["size_bytes"] <= settings.MAX_UPLOAD_BYTES):
            raise BrokerError("INVALID_REQUEST", "Use a SHA-256 digest and bounded byte count for each object.")
        descriptors.append((entry["sha256"], entry["size_bytes"]))
        size += entry["size_bytes"]
    if size > 16 * 1024 * 1024 or len(set(descriptors)) != len(descriptors):
        raise BrokerError("INVALID_REQUEST", "Content lookup exceeds its bounds or repeats an object.")
    blobs = {blob.sha256: blob for blob in Blob.objects.filter(project=project,
        sha256__in=[entry[0] for entry in descriptors])}
    present = []
    for digest, size in descriptors:
        blob = blobs.get(digest)
        if blob and blob.size_bytes == size:
            try:
                read_blob(blob)
            except OSError:
                raise BrokerError("OBJECT_UNAVAILABLE", "Stored object is unavailable; retry after storage recovery.", 409) from None
            present.append({"sha256": digest, "size_bytes": size})
    from django.http import JsonResponse
    response = JsonResponse({"present": present})
    response["Cache-Control"] = "no-store, private"
    return response
