"""Check examples against the packaged schemas and shared offline validator."""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from compute_relay.domain.validation import strict_json, validate, validators

CONTRACTS = ROOT / "contracts"
EXAMPLES = {
    "cpu-job.json": "job-request.schema.json",
    "gpu-job.json": "job-request.schema.json",
    "prepared-gpu-job.json": "prepared-job.schema.json",
    "project-policy.json": "project-policy.schema.json",
}


def main():
    schemas = validators()
    actual = {path.name for path in (CONTRACTS / "examples").glob("*.json")}
    if actual != set(EXAMPLES):
        raise ValueError("Every example must have an explicit schema mapping")
    for filename, schema_name in EXAMPLES.items():
        validate(schema_name, strict_json((CONTRACTS / "examples" / filename).read_bytes()))
    print(f"Validated {len(schemas)} packaged schemas and {len(EXAMPLES)} examples offline.")


if __name__ == "__main__":
    main()
