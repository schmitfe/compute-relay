"""Bounded authenticated HTTP with no credential forwarding through redirects."""

from __future__ import annotations

import json
import os
import re
import ssl
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request, build_opener


_RESOURCE_FIELDS = {
    "cpu_cores": ("CPU cores", "cores"),
    "gpu_devices": ("GPU devices", "devices"),
    "memory_mib": ("Memory", "MiB"),
    "time_limit_seconds": ("Walltime", "seconds"),
}
_MAX_ERROR_BYTES = 64 * 1024


def _public_resource_limit_details(details):
    """Accept only fixed resource names and checked integers, never remote prose."""
    if not isinstance(details, dict):
        return None
    violations = details.get("violations")
    if not isinstance(violations, list) or not 1 <= len(violations) <= 8:
        return None
    public = []
    seen = set()
    for item in violations:
        if not isinstance(item, dict):
            return None
        resource, source = item.get("resource"), item.get("source")
        if (not isinstance(resource, str) or resource not in _RESOURCE_FIELDS
                or not isinstance(source, str) or source not in {"project", "cluster"}
                or item.get("unit") != _RESOURCE_FIELDS[resource][1]):
            return None
        numbers = {key: item.get(key) for key in ("requested", "effective", "limit", "excess")}
        if any(type(value) is not int or not 0 <= value <= 2**53 - 1 for value in numbers.values()):
            return None
        if (numbers["effective"] <= numbers["limit"]
                or numbers["excess"] != numbers["effective"] - numbers["limit"]
                or (resource, source) in seen):
            return None
        seen.add((resource, source))
        public.append({"resource": resource, **numbers, "source": source,
                       "unit": _RESOURCE_FIELDS[resource][1]})
    return {"violations": public}


class ControlError(RuntimeError):
    def __init__(self, code: str, status: int | None = None, *, broker_code=None, details=None):
        self.code = code
        self.status = status
        self.broker_code = None
        self.details = {}
        message = code
        public = _public_resource_limit_details(details) if broker_code == "RESOURCE_LIMIT" else None
        if public:
            self.broker_code = "RESOURCE_LIMIT"
            self.details = public
            violations = []
            for item in public["violations"]:
                label, unit = _RESOURCE_FIELDS[item["resource"]]
                violations.append(
                    f"{label}: effective {item['effective']} {unit}, "
                    f"limit {item['limit']} {unit} ({item['source']}; "
                    f"requested {item['requested']} {unit}; excess {item['excess']} {unit})"
                )
            message += ": RESOURCE_LIMIT. " + "; ".join(violations)
            message += ". Refresh list_compute_targets and get_project_allowance, then revise the resources before retrying."
        super().__init__(message)


def _http_error(status, content):
    """Error responses are private except for this narrow numeric contract."""
    try:
        document = strict_json(content)
        error = document.get("error") if isinstance(document, dict) else None
        if isinstance(error, dict) and error.get("code") == "RESOURCE_LIMIT":
            return ControlError("CONTROL_HTTP_ERROR", status, broker_code="RESOURCE_LIMIT",
                                details=error.get("details"))
    except (ValueError, UnicodeError, RecursionError):
        pass
    return ControlError("CONTROL_HTTP_ERROR", status)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def strict_json(data: bytes):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON property")
            result[key] = value
        return result

    def invalid_number(value):
        raise ValueError("nonintegral JSON number")

    value = json.loads(data.decode("utf-8"), object_pairs_hook=pairs,
                       parse_constant=invalid_number, parse_float=invalid_number)

    def check(item):
        if isinstance(item, str):
            item.encode("utf-8", errors="strict")
        elif isinstance(item, bool) or item is None:
            return
        elif isinstance(item, int):
            if abs(item) > 2**53 - 1:
                raise ValueError("inexact JSON integer")
        elif isinstance(item, list):
            for entry in item:
                check(entry)
        elif isinstance(item, dict):
            for key, entry in item.items():
                check(key)
                check(entry)
        else:
            raise ValueError("unsupported JSON value")

    check(value)
    return value


def environment_token(name: str) -> str:
    """Read one explicitly configured credential, never print it or search homes."""
    value = os.environ.get(name)
    filename = os.environ.get(name + "_FILE")
    if value and filename:
        raise ValueError("configure either token or token file")
    if filename:
        value = Path(filename).read_text(encoding="utf-8").strip()
    if not value or any(c.isspace() for c in value):
        raise ValueError("missing or malformed authentication token")
    return value


class JsonClient:
    def __init__(self, base_url: str, token: str, *, allow_insecure_http: bool = False,
                 timeout: float = 30, max_response_bytes: int = 4 * 1024 * 1024):
        parts = urlsplit(base_url)
        if (parts.username or parts.password or parts.query or parts.fragment
                or parts.path not in ("", "/") or not parts.hostname):
            raise ValueError("control URL must be an origin without credentials")
        loopback = parts.hostname in {"localhost", "127.0.0.1", "::1"}
        if parts.scheme != "https" and not (
            parts.scheme == "http" and (loopback or allow_insecure_http)
        ):
            raise ValueError("HTTPS is required for a remote control service")
        if not token or any(c.isspace() for c in token):
            raise ValueError("invalid authentication token")
        self.base_url = base_url.rstrip("/")
        self._token = token
        self.timeout = timeout
        self.max_response_bytes = max_response_bytes
        self._opener = build_opener(ProxyHandler({}), _NoRedirect(),
                                    HTTPSHandler(context=ssl.create_default_context()))

    def _request(self, method, path, body=None, *, idempotency_key=None, limit=None):
        if not path.startswith("/") or path.startswith("//") or "\r" in path or "\n" in path:
            raise ValueError("request path must be origin-relative")
        headers = {"Authorization": "Bearer " + self._token, "Accept": "application/json"}
        if idempotency_key:
            if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", idempotency_key):
                raise ValueError("invalid idempotency key")
            headers["Idempotency-Key"] = idempotency_key
        data = None
        if body is not None:
            data = json.dumps(body, allow_nan=False, ensure_ascii=False,
                              separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(self.base_url + path, data=data, headers=headers, method=method)
        maximum = limit if limit is not None else self.max_response_bytes
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                content = response.read(maximum + 1)
                if len(content) > maximum:
                    raise ControlError("RESPONSE_TOO_LARGE")
                return content, response.headers.get("Content-Type", "application/octet-stream")
        except HTTPError as exc:
            status = exc.code
            error = ControlError("CONTROL_HTTP_ERROR", status)
            try:
                # Bound reads before parsing; never include the remote message.
                maximum_error = min(maximum, _MAX_ERROR_BYTES)
                content = exc.read(maximum_error + 1)
                if len(content) <= maximum_error:
                    error = _http_error(status, content)
            except (OSError, ValueError):
                pass
            finally:
                exc.close()
            raise error from None
        except (URLError, TimeoutError, OSError):
            raise ControlError("CONTROL_UNAVAILABLE") from None

    def request(self, method, path, body=None, *, idempotency_key=None):
        content, _ = self._request(method, path, body, idempotency_key=idempotency_key)
        try:
            return strict_json(content) if content else {}
        except (ValueError, UnicodeError, RecursionError):
            raise ControlError("INVALID_CONTROL_RESPONSE") from None

    def get_bytes(self, path: str, limit: int):
        return self._request("GET", path, limit=limit)
