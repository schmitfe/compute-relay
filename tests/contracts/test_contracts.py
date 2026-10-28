"""Executable planning-contract cases; no SSH, broker, database or Slurm runtime."""

from copy import deepcopy
import sys
import unittest
from pathlib import Path

from jsonschema import ValidationError
from referencing.exceptions import NoSuchResource

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
from check_contracts import CONTRACTS, EXAMPLES, main  # noqa: E402
from compute_relay.domain.validation import _deny_remote, strict_json, validate


class PlanningContractTests(unittest.TestCase):
    def setUp(self):
        self.request = strict_json((CONTRACTS / "examples" / "gpu-job.json").read_bytes())
        self.prepared = strict_json((CONTRACTS / "examples" / "prepared-gpu-job.json").read_bytes())

    def reject_request(self, data):
        with self.assertRaises((ValueError, ValidationError)):
            validate("job-request.schema.json", data)

    def reject_prepared(self, data):
        with self.assertRaises((ValueError, ValidationError)):
            validate("prepared-job.schema.json", data)

    def test_all_documented_examples_validate(self):
        for filename, schema in EXAMPLES.items():
            with self.subTest(filename=filename):
                validate(schema, strict_json((CONTRACTS / "examples" / filename).read_bytes()))

    def test_example_inventory_is_complete(self):
        main()

    def test_large_input_descriptors_use_the_runtime_limit_in_prepared_packages(self):
        data = deepcopy(self.prepared)
        source = data["request"]["files"][0]
        source["size_bytes"] = 300 * 1024**2
        next(entry for entry in data["files"] if entry["path"] == source["path"])["size_bytes"] = source["size_bytes"]
        validate("job-request.schema.json", data["request"])
        validate("prepared-job.schema.json", data)

    def test_aggregate_input_limit_remains_bounded(self):
        data = deepcopy(self.request)
        source = data["files"][0]
        data["files"] = [{**source, "path": f"input{index}.py", "size_bytes": 8 * 1024**3} for index in range(9)]
        data["command"]["script"] = data["files"][0]["path"]
        self.reject_request(data)

    def test_credential_and_authority_fields_are_not_job_inputs(self):
        for field in ("ssh_private_key", "host", "ssh_options", "approved", "budget_override", "sbatch_args"):
            with self.subTest(field=field):
                data = deepcopy(self.request)
                data[field] = "untrusted"
                self.reject_request(data)

    def test_scheduler_newline_injection_is_rejected(self):
        data = deepcopy(self.request)
        data["slurm"]["partition"] = "gpu\n#SBATCH --time=99:00:00"
        self.reject_request(data)

    def test_native_scheduler_options_are_rejected(self):
        data = deepcopy(self.request)
        data["slurm"]["requeue"] = True
        self.reject_request(data)

    def test_paths_reject_escape_and_ambiguous_names(self):
        for path in ("/etc/passwd", "../secret", "a/../b", "a/./b", "a//b", "a/", "a\\b", "a\nfile", "a/%2e%2e/b", "-option"):
            with self.subTest(path=path):
                data = deepcopy(self.request)
                data["files"][0]["path"] = path
                data["command"]["script"] = path
                self.reject_request(data)

    def test_broker_namespace_is_reserved(self):
        data = deepcopy(self.request)
        data["outputs"] = ["_broker/batch.sh"]
        self.reject_request(data)

    def test_entrypoint_must_be_in_package(self):
        data = deepcopy(self.request)
        data["command"]["script"] = "missing.py"
        self.reject_request(data)

    def test_duplicate_path_with_different_digest_is_rejected(self):
        data = deepcopy(self.request)
        other = deepcopy(data["files"][0])
        other["sha256"] = "f" * 64
        data["files"].append(other)
        self.reject_request(data)

    def test_outputs_cannot_replace_uploaded_code(self):
        data = deepcopy(self.request)
        data["outputs"] = [data["command"]["script"]]
        self.reject_request(data)

    def test_gpu_type_is_required_and_cpu_only_type_is_rejected(self):
        data = deepcopy(self.request)
        del data["resources"]["gpu_type"]
        self.reject_request(data)
        data = deepcopy(self.request)
        data["resources"]["gpus"] = 0
        self.reject_request(data)

    def test_resource_limits_have_no_boolean_or_fractional_coercion(self):
        for value in (True, "8", 1.5, 0, -1):
            with self.subTest(value=value):
                data = deepcopy(self.request)
                data["resources"]["cpus"] = value
                self.reject_request(data)

    def test_mvp_rejects_multi_node_and_unbounded_time(self):
        data = deepcopy(self.request)
        data["resources"]["nodes"] = 2
        self.reject_request(data)
        data = deepcopy(self.request)
        del data["resources"]["time_limit_seconds"]
        self.reject_request(data)

    def test_scheduler_walltime_requires_whole_minutes(self):
        data = deepcopy(self.request)
        data["resources"]["time_limit_seconds"] = 61
        self.reject_request(data)

    def test_prepared_files_preserve_uploaded_descriptors(self):
        data = deepcopy(self.prepared)
        data["files"][-1]["sha256"] = "f" * 64
        self.reject_prepared(data)

    def test_prepared_manifest_cannot_omit_wrapper(self):
        data = deepcopy(self.prepared)
        data["files"] = [f for f in data["files"] if f["path"] != data["environment_wrapper"]]
        self.reject_prepared(data)

    def test_reservation_covers_cpu_and_gpu_allocation_seconds(self):
        for dimension in ("cpu_core_seconds", "gpu_device_seconds"):
            with self.subTest(dimension=dimension):
                data = deepcopy(self.prepared)
                data["reservation"][dimension] -= 1
                self.reject_prepared(data)

    def test_allocation_cannot_be_below_request(self):
        data = deepcopy(self.prepared)
        data["maximum_allocation"]["cpu_cores"] = 1
        self.reject_prepared(data)

    def test_rounding_up_allocation_requires_corresponding_reservation(self):
        data = deepcopy(self.prepared)
        data["maximum_allocation"]["cpu_cores"] = 16
        self.reject_prepared(data)
        data["reservation"]["cpu_core_seconds"] = 16 * data["maximum_allocation"]["time_limit_seconds"]
        validate("prepared-job.schema.json", data)

    def test_new_packages_reserve_requested_walltime_without_added_duration(self):
        data = deepcopy(self.prepared)
        data["reservation"]["cpu_core_seconds"] = 8 * data["maximum_allocation"]["time_limit_seconds"]
        data["reservation"]["gpu_device_seconds"] = data["maximum_allocation"]["time_limit_seconds"]
        validate("prepared-job.schema.json", data)
        data["reservation"]["cpu_core_seconds"] += 8 * 60
        data["reservation"]["gpu_device_seconds"] += 60
        self.reject_prepared(data)

    def test_policy_rejects_a_separate_charge_duration(self):
        data = strict_json((CONTRACTS / "examples" / "project-policy.json").read_bytes())
        self.assertNotIn("maximum_charge_duration_seconds", data["per_job_limits"])
        validate("project-policy.schema.json", data)
        data["per_job_limits"]["maximum_charge_duration_seconds"] = 60
        with self.assertRaises(ValidationError):
            validate("project-policy.schema.json", data)

    def test_connection_revision_is_required(self):
        data = deepcopy(self.prepared)
        del data["connection_revision_sha256"]
        self.reject_prepared(data)

    def test_wire_counters_remain_exact_in_browser_json(self):
        data = strict_json((CONTRACTS / "examples" / "project-policy.json").read_bytes())
        data["allowance"]["cpu_core_seconds"] = 2**53 - 1
        validate("project-policy.schema.json", data)
        data["allowance"]["cpu_core_seconds"] = 2**53
        with self.assertRaises(ValidationError):
            validate("project-policy.schema.json", data)

    def test_loader_rejects_duplicate_keys_and_nonfinite_numbers(self):
        for contents in ('{"approved": false, "approved": true}', '{"n": NaN}', '{"n": Infinity}'):
            with self.subTest(contents=contents), self.assertRaises(ValueError):
                strict_json(contents)

    def test_unpaired_unicode_surrogate_is_rejected(self):
        data = deepcopy(self.request)
        data["command"]["arguments"] = ["\ud800"]
        self.reject_request(data)

    def test_policy_rejects_malformed_expiry(self):
        data = strict_json((CONTRACTS / "examples" / "project-policy.json").read_bytes())
        data["admission_expires_at"] = "sometime"
        with self.assertRaises(ValidationError):
            validate("project-policy.schema.json", data)

    def test_remote_schema_resolution_is_disabled(self):
        with self.assertRaises(NoSuchResource):
            _deny_remote("https://example.invalid/untrusted.schema.json")


if __name__ == "__main__":
    unittest.main()
