"""Strict offline validation shared by the service and clients."""

import json
from functools import lru_cache
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource
from referencing.exceptions import NoSuchResource

from compute_relay.transfer import MAX_BUNDLE_BYTES


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _nonfinite(value):
    raise ValueError("Nonfinite JSON number")


def check_unicode(value):
    if isinstance(value, str):
        if any(0xD800 <= ord(c) <= 0xDFFF for c in value):
            raise ValueError("Unpaired Unicode surrogate")
    elif isinstance(value, dict):
        for key, item in value.items():
            check_unicode(key)
            check_unicode(item)
    elif isinstance(value, list):
        for item in value:
            check_unicode(item)


def strict_json(data):
    result = json.loads(data, object_pairs_hook=_pairs, parse_constant=_nonfinite)
    check_unicode(result)
    return result


def _deny_remote(uri):
    raise NoSuchResource(ref=uri)


@lru_cache
def validators():
    schemas = {}
    registry = Registry(retrieve=_deny_remote)
    for path in (Path(__file__).resolve().parents[1] / "schemas").glob("*.schema.json"):
        schema = strict_json(path.read_text())
        Draft202012Validator.check_schema(schema)
        schemas[path.name] = schema
        registry = registry.with_resource(schema["$id"], Resource.from_contents(schema))
    return {name: Draft202012Validator(schema, registry=registry, format_checker=FormatChecker()) for name, schema in schemas.items()}


def _files_by_path(files):
    indexed = {entry["path"]: entry for entry in files}
    if len(indexed) != len(files):
        raise ValueError("Duplicate logical file path")
    if sum(entry["size_bytes"] for entry in files) > MAX_BUNDLE_BYTES:
        raise ValueError("Package too large")
    return indexed


def validate(schema_name, document):
    check_unicode(document)
    validators()[schema_name].validate(document)
    if schema_name == "job-request.schema.json":
        files = _files_by_path(document["files"])
        if document["command"]["script"] not in files:
            raise ValueError("Entrypoint is not an uploaded file")
        if set(files).intersection(document["outputs"]):
            raise ValueError("Output overwrites an input")
        for path in [*files, *document["outputs"]]:
            if path == "_broker" or path.startswith("_broker/"):
                raise ValueError("Reserved broker namespace")
            if path.count("/") > 16:
                raise ValueError("Logical path nesting exceeds limit")
    elif schema_name == "prepared-job.schema.json":
        request = document["request"]
        validate("job-request.schema.json", request)
        files = _files_by_path(document["files"])
        if list(files) != sorted(files):
            raise ValueError("Prepared file descriptors must be sorted by path")
        for entry in request["files"]:
            if files.get(entry["path"]) != entry:
                raise ValueError("Prepared package changed or omitted an uploaded file")
        generated = set(files) - {entry["path"] for entry in request["files"]}
        if generated != {document["batch_script"], document["environment_wrapper"]}:
            raise ValueError("Unexpected or missing generated files in version one package")
        allocation, requested = document["maximum_allocation"], request["resources"]
        if allocation["cpu_cores"] < requested["cpus"] or allocation["gpu_devices"] < requested["gpus"]:
            raise ValueError("Allocation envelope is below requested resources")
        if allocation["gpu_devices"] and not requested["gpus"]:
            raise ValueError("GPU allocation requires a positive GPU request")
        if allocation["time_limit_seconds"] != requested["time_limit_seconds"]:
            raise ValueError("Walltime changed after request preparation")
        duration = requested["time_limit_seconds"]
        if document["reservation"] != {"cpu_core_seconds": allocation["cpu_cores"] * duration,
                                       "gpu_device_seconds": allocation["gpu_devices"] * duration}:
            raise ValueError("Reservation must cover allocated resources multiplied by requested walltime")
    elif schema_name == "project-policy.schema.json":
        if document["per_job_limits"]["gpu_devices"] and not document["allowed_gpu_types"]:
            raise ValueError("GPU-enabled policy needs explicit GPU types")


def relative_path(value):
    schema = validators()["common.schema.json"].schema["$defs"]["relativePath"]
    Draft202012Validator(schema).validate(value)
    if value.count("/") > 16:
        raise ValueError("Path nesting exceeds limit")
    return value
