"""Strict offline validation shared by the service and clients."""

import json
from functools import lru_cache
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource
from referencing.exceptions import NoSuchResource


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


def validate(schema_name, document):
    check_unicode(document)
    validators()[schema_name].validate(document)
    if schema_name == "job-request.schema.json":
        files = {f["path"]: f for f in document["files"]}
        if len(files) != len(document["files"]):
            raise ValueError("Duplicate logical file path")
        if document["command"]["script"] not in files:
            raise ValueError("Entrypoint is not an uploaded file")
        if set(files).intersection(document["outputs"]):
            raise ValueError("Output overwrites an input")
        for path in [*files, *document["outputs"]]:
            if path == "_broker" or path.startswith("_broker/"):
                raise ValueError("Reserved broker namespace")
            if path.count("/") > 16:
                raise ValueError("Logical path nesting exceeds limit")
        if sum(f["size_bytes"] for f in files.values()) > 64 * 1024**3:
            raise ValueError("Package too large")


def relative_path(value):
    schema = validators()["common.schema.json"].schema["$defs"]["relativePath"]
    Draft202012Validator(schema).validate(value)
    if value.count("/") > 16:
        raise ValueError("Path nesting exceeds limit")
    return value
