"""Finite transfer steps for the asynchronous execution worker."""

import asyncio
import base64
import random

from compute_relay.transfer import CHUNK_BYTES, MAX_BUNDLE_BYTES, MAX_FILE_BYTES, FileReference
from .client import ControlError


def authority(lease):
    return {"lease_id": lease["id"], "generation": lease["generation"]}


def check_offset(reply, size):
    offset = reply.get("offset")
    if type(offset) is not int or not 0 <= offset <= size or type(reply.get("complete")) is not bool:
        raise ValueError("Invalid transfer acknowledgement")
    return offset


def check_chunk(reply, offset, size):
    content = base64.b64decode(reply["content_base64"], validate=True)
    if reply.get("offset") != offset or len(content) != min(CHUNK_BYTES, size - offset):
        raise ValueError("Invalid transfer chunk")
    return content


def output_entries(inventory, manifest):
    files = inventory["files"]
    permitted = set(manifest["request"]["outputs"])
    if not isinstance(files, list) or len(files) > len(permitted):
        raise ValueError("Invalid output inventory")
    seen, total = set(), 0
    for entry in files:
        path, size = entry["path"], entry["size_bytes"]
        if path not in permitted or path in seen or type(size) is not int or not 0 <= size <= MAX_FILE_BYTES:
            raise ValueError("Invalid output descriptor")
        seen.add(path)
        total += size
        if total > MAX_BUNDLE_BYTES:
            raise ValueError("Output transfer exceeds aggregate limit")
    return files


async def stage_inputs(client, backend, lease, manifest, files):
    for path, source in files.items():
        if not isinstance(source, FileReference):
            continue
        reply = await backend.stage_input(lease["attempt_id"], manifest, path)
        offset = check_offset(reply, source.size_bytes)
        while not reply["complete"]:
            chunk = await client.request("POST", f"/internal/v1/attempts/{lease['attempt_id']}/input-chunk",
                {**authority(lease), "path": path, "offset": offset})
            content = check_chunk(chunk, offset, source.size_bytes)
            reply = await backend.stage_input(lease["attempt_id"], manifest, path, offset=offset,
                content_base64=chunk["content_base64"], finish=offset + len(content) == source.size_bytes)
            new_offset = check_offset(reply, source.size_bytes)
            if new_offset != offset + len(content):
                raise ValueError("Invalid input acknowledgement")
            offset = new_offset


async def _publish_output(client, endpoint, body, publication_lock):
    for attempt in range(8):
        try:
            async with publication_lock:
                return await client.request("POST", endpoint, body)
        except ControlError as error:
            if error.status != 409 or error.broker_code != "UPLOAD_BUSY" or attempt == 7:
                raise
            await asyncio.sleep(random.uniform(.05, min(2, .1 * 2**attempt)))


async def collect_outputs(client, backend, lease, native, manifest, publication_lock):
    inventory = await backend.output_manifest(native, manifest=manifest)
    for entry in output_entries(inventory, manifest):
        endpoint = f"/internal/v1/attempts/{lease['attempt_id']}/output-chunk"
        common = {**authority(lease), "path": entry["path"]}
        reply = await _publish_output(client, endpoint, {**common, "sha256": entry["sha256"], "size_bytes": entry["size_bytes"]}, publication_lock)
        offset = check_offset(reply, entry["size_bytes"])
        while not reply["complete"]:
            chunk = await backend.output_chunk(native, manifest=manifest, path=entry["path"], offset=offset, version=entry["version"])
            content = check_chunk(chunk, offset, entry["size_bytes"])
            reply = await _publish_output(client, endpoint, {**common, "transfer_id": reply["transfer_id"], "offset": offset,
                "content_base64": chunk["content_base64"], "finish": offset + len(content) == entry["size_bytes"]}, publication_lock)
            new_offset = check_offset(reply, entry["size_bytes"])
            if new_offset != offset + len(content):
                raise ValueError("Invalid output acknowledgement")
            offset = new_offset
    return {"artifacts": {}, "collection_state": "failed" if inventory.get("missing") else "complete",
        **({"error_code": "OUTPUT_MISSING"} if inventory.get("missing") else {})}
