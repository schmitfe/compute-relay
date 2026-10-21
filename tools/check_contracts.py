"""Check planning schemas and examples offline; this is not broker runtime code."""

from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource
from referencing.exceptions import NoSuchResource

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "contracts"
EXAMPLES = {
    "cpu-job.json": "job-request.schema.json",
    "gpu-job.json": "job-request.schema.json",
    "prepared-gpu-job.json": "prepared-job.schema.json",
    "project-policy.json": "project-policy.schema.json",
}


def reject_duplicate_keys(pairs: list[tuple]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON object key: {key}")
        result[key] = value
    return result


def reject_nonfinite(value: str):
    raise ValueError(f"Nonfinite JSON number: {value}")


def check_unicode(value) -> None:
    if isinstance(value, str):
        if any(0xD800 <= ord(char) <= 0xDFFF for char in value):
            raise ValueError("Unpaired Unicode surrogate is not canonical JSON")
    elif isinstance(value, dict):
        for key, item in value.items():
            check_unicode(key)
            check_unicode(item)
    elif isinstance(value, list):
        for item in value:
            check_unicode(item)


def load_json(path: Path) -> dict:
    data = json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=reject_duplicate_keys,
        parse_constant=reject_nonfinite,
    )
    check_unicode(data)
    return data


def no_remote_retrieval(uri: str) -> Resource:
    raise NoSuchResource(ref=uri)


def schema_registry() -> tuple[dict[str, dict], Registry]:
    schemas = {}
    registry = Registry(retrieve=no_remote_retrieval)
    for path in sorted(CONTRACTS.glob("*.schema.json")):
        schema = load_json(path)
        Draft202012Validator.check_schema(schema)
        schemas[path.name] = schema
        registry = registry.with_resource(schema["$id"], Resource.from_contents(schema))
    return schemas, registry


def files_by_path(files: list[dict]) -> dict[str, dict]:
    result = {item["path"]: item for item in files}
    if len(result) != len(files):
        raise ValueError("Duplicate logical file path")
    if sum(item["size_bytes"] for item in files) > 256 * 1024 * 1024:
        raise ValueError("Package exceeds 256 MiB contract limit")
    return result


def validate_request(data: dict) -> None:
    files = files_by_path(data["files"])
    paths = [*files, *data["outputs"]]
    if any(path == "_broker" or path.startswith("_broker/") for path in paths):
        raise ValueError("The _broker namespace is reserved")
    if data["command"]["script"] not in files:
        raise ValueError("Entrypoint must be an uploaded file")
    if set(files).intersection(data["outputs"]):
        raise ValueError("An output must not overwrite an input")


def validate_prepared(data: dict) -> None:
    request = data["request"]
    validate_request(request)
    files = files_by_path(data["files"])
    if list(files) != sorted(files):
        raise ValueError("Prepared file descriptors must be sorted by path")
    source_files = files_by_path(request["files"])
    for path, descriptor in source_files.items():
        if files.get(path) != descriptor:
            raise ValueError("Prepared package changed or omitted an uploaded file")
    generated = set(files) - set(source_files)
    if generated != {data["batch_script"], data["environment_wrapper"]}:
        raise ValueError("Unexpected or missing generated files in version one package")
    allocation = data["maximum_allocation"]
    requested = request["resources"]
    if allocation["cpu_cores"] < requested["cpus"]:
        raise ValueError("Allocation envelope is below requested CPU count")
    if allocation["gpu_devices"] < requested["gpus"]:
        raise ValueError("Allocation envelope is below requested GPU count")
    if allocation["gpu_devices"] and not requested.get("gpu_type"):
        raise ValueError("GPU allocation requires an explicit GPU type")
    if allocation["time_limit_seconds"] != requested["time_limit_seconds"]:
        raise ValueError("Walltime changed after request preparation")
    duration = allocation["maximum_charge_duration_seconds"]
    if duration < allocation["time_limit_seconds"]:
        raise ValueError("Maximum charge duration is below scheduler walltime")
    expected = {
        "cpu_core_seconds": allocation["cpu_cores"] * duration,
        "gpu_device_seconds": allocation["gpu_devices"] * duration,
    }
    if data["reservation"] != expected:
        raise ValueError("Reservation must cover the complete maximum allocation")


def validate_document(schema_name: str, data: dict) -> None:
    check_unicode(data)
    schemas, registry = schema_registry()
    Draft202012Validator(
        schemas[schema_name], registry=registry, format_checker=FormatChecker()
    ).validate(data)
    if schema_name == "job-request.schema.json":
        validate_request(data)
    elif schema_name == "prepared-job.schema.json":
        validate_prepared(data)
    elif schema_name == "project-policy.schema.json":
        if data["per_job_limits"]["gpu_devices"] and not data["allowed_gpu_types"]:
            raise ValueError("GPU-enabled policy needs explicit GPU types")


def main() -> None:
    schemas, _ = schema_registry()
    actual = {path.name for path in (CONTRACTS / "examples").glob("*.json")}
    if actual != set(EXAMPLES):
        raise ValueError("Every example must have an explicit schema mapping")
    for filename, schema_name in EXAMPLES.items():
        validate_document(schema_name, load_json(CONTRACTS / "examples" / filename))
    print(f"Validated {len(schemas)} planning schemas and {len(EXAMPLES)} examples offline.")


if __name__ == "__main__":
    main()
