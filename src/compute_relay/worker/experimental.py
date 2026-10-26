"""Operator-only switches for the batched/persistent transport experiment."""

import os


def status_batch_size():
    try:
        value = int(os.environ.get("COMPUTE_RELAY_EXPERIMENTAL_STATUS_BATCH_SIZE", "1"))
    except ValueError:
        raise ValueError("Experimental status batch must be from 1 through 64") from None
    if not 1 <= value <= 64:
        raise ValueError("Experimental status batch must be from 1 through 64")
    return value


def persistent_enabled():
    value = os.environ.get("COMPUTE_RELAY_EXPERIMENTAL_PERSISTENT_HELPER", "0")
    if value not in {"0", "1"}:
        raise ValueError("Experimental persistent helper must be 0 or 1")
    return value == "1"
