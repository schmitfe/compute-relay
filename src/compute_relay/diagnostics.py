"""Failure context without exception text, remote output or credentials."""

from contextlib import contextmanager
from contextvars import ContextVar
import logging
import time
from uuid import UUID

OPERATIONS = {"submit", "reconcile", "observe", "cancel", "cleanup", "discover", "availability", "extension", "setup"}
PHASES = {"poll", "profiles", "validate", "backend", "observe", "execute", "logs", "finalize", "collect", "report", "renew", "submit", "cancel", "cleanup", "discover", "availability", "extension", "remote", "setup"}
_CURRENT = ContextVar("worker_diagnostic_lease", default=None)


def _uuid(value):
    if isinstance(value, str) and len(value) == 36:
        try:
            return str(UUID(value))
        except ValueError:
            pass
    return None


def failure(lease, phase, error, started):
    from compute_relay.transports import TransportError
    from compute_relay.worker.client import ControlError
    from compute_relay.worker.enrollment import EnrollmentError
    errors = ((ControlError, "control"), (EnrollmentError, "configuration"), (TransportError, "transport"),
        (TimeoutError, "timeout"), (OSError, "io"), (ValueError, "validation"), (KeyError, "validation"), (TypeError, "validation"))
    lease = _CURRENT.get() if lease is None else lease
    lease = lease if isinstance(lease, dict) else {}
    kind, category = next(((kind, category) for kind, category in errors if isinstance(error, kind)), (Exception, "unexpected"))
    generation, leased_operation = lease.get("generation"), lease.get("operation")
    context = {"operation": leased_operation if isinstance(leased_operation, str) and leased_operation in OPERATIONS else "unknown",
        "phase": phase if isinstance(phase, str) and phase in PHASES else "unknown", "attempt_id": _uuid(lease.get("attempt_id")),
        "connection_id": _uuid(lease.get("connection_id")),
        "generation": generation if type(generation) is int and 0 <= generation <= 2**53 - 1 else None,
        "category": category, "error_type": kind.__name__, "duration_ms": max(0, round((time.monotonic() - started) * 1000))}
    logging.getLogger(__name__).warning(
        "Worker failure operation=%(operation)s phase=%(phase)s attempt_id=%(attempt_id)s "
        "connection_id=%(connection_id)s generation=%(generation)s category=%(category)s "
        "error_type=%(error_type)s duration_ms=%(duration_ms)s", context, extra=context)


@contextmanager
def operation(lease, phase):
    lease = _CURRENT.get() if lease is None else lease
    token = _CURRENT.set(lease)
    started = time.monotonic()
    try:
        yield
    except Exception as error:
        failure(lease, phase, error, started)
        raise
    finally:
        _CURRENT.reset(token)
