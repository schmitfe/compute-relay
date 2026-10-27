"""Bounded byte-accurate log windows with explicit gaps and stream identity."""

import base64

from django.utils import timezone


def apply_logs(attempt, chunks):
    if not isinstance(chunks, dict) or set(chunks) - {"stdout", "stderr"}:
        raise ValueError("Invalid log streams")
    state = dict(attempt.log_state)
    for stream, packet in chunks.items():
        content = base64.b64decode(packet["content_base64"], validate=True)
        start, end, size = packet["cursor"], packet["next_cursor"], packet["size_bytes"]
        file_id = packet["file_id"]
        if (any(type(value) is not int or not 0 <= value <= 2**53 - 1 for value in (start, end, size))
                or end != start + len(content) or end > size or len(content) > 65536
                or not isinstance(file_id, str) or len(file_id) > 100
                or type(packet.get("reset")) is not bool or type(packet.get("truncated")) is not bool):
            raise ValueError("Invalid log window")
        previous = state.get(stream, {})
        if (previous and previous.get("file_id") == file_id and not packet["reset"]
                and start == previous["next_cursor"]):
            content = base64.b64decode(previous["content_base64"], validate=True) + content
        content = content[-65536:]
        changed = end != previous.get("next_cursor") or file_id != previous.get("file_id")
        state[stream] = {"cursor": end - len(content), "next_cursor": end, "size_bytes": size,
            "file_id": file_id, "content_base64": base64.b64encode(content).decode("ascii"),
            "updated_at": timezone.now().isoformat() if changed else previous.get("updated_at"),
            "observed_at": timezone.now().isoformat()}
        setattr(attempt, stream, content.decode("utf-8", "replace"))
    attempt.log_state = state


def log_window(attempt, stream, cursor, limit):
    state = attempt.log_state.get(stream)
    if state:
        content = base64.b64decode(state["content_base64"], validate=True)
        beginning, end = state["cursor"], state["next_cursor"]
        reset = cursor > end
        start = beginning if reset else max(beginning, cursor)
        chunk = content[start - beginning:start - beginning + limit]
    else:
        content = getattr(attempt, stream).encode("utf-8")
        beginning, end, reset = 0, len(content), False
        start = min(cursor, end)
        chunk = content[start:start + limit]
    next_cursor = start + len(chunk)
    return {"stream": stream, "content_base64": base64.b64encode(chunk).decode("ascii"),
        "text": chunk.decode("utf-8", "replace"), "cursor": start, "next_cursor": next_cursor,
        "eof": next_cursor >= end, "finished": attempt.state in {"succeeded", "failed", "cancelled", "expired"},
        "truncated": cursor < beginning, "reset": reset, "available_from": beginning,
        "updated_at": state.get("updated_at") if state else None,
        "observed_at": state.get("observed_at") if state else None,
        "file_id": state.get("file_id") if state else None}
