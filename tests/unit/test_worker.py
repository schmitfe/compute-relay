import base64
import copy
import hashlib
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

from compute_broker.worker.client import ControlError, JsonClient, strict_json
from compute_broker.worker.journal import Journal
from compute_broker.worker.runner import Worker
from compute_broker.worker.enrollment import EnrollmentError, profile_metadata, resolve_connection


class FakeControl:
    def __init__(self):
        self.calls = []
        self.fail_report = False

    def request(self, method, path, body=None, **kwargs):
        self.calls.append((method, path, body))
        if path.endswith("/dispatch"):
            return {"authorized": True}
        if path.endswith("/report") and self.fail_report:
            self.fail_report = False
            raise ControlError("CONTROL_UNAVAILABLE")
        return {}


class BackendSpy:
    def __init__(self):
        self.submitted = 0
        self.reconciled = 0
        self.failure = False

    def submit(self, attempt_id, manifest, files):
        self.submitted += 1
        if self.failure:
            raise TimeoutError("sensitive remote diagnostics must not reach a report")
        return {"outcome": "submitted", "native_identity": {"job_id": "17", "attempt_id": attempt_id}}

    def reconcile(self, attempt_id, manifest):
        self.reconciled += 1
        return {"outcome": "submission_uncertain"}

    def observe(self, native):
        return {"state": "completed", "terminal": True,
                "accounting": {"cpu_core_seconds": 2, "gpu_device_seconds": 0, "final": True}}

    def collect(self, native, outputs):
        return {"stdout": "synthetic result", "stderr": "", "artifacts": {}}


class WorkerTests(unittest.TestCase):
    def setUp(self):
        from compute_broker.backends import prepare

        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.control = FakeControl()
        self.backend = BackendSpy()
        self.connection_id = str(uuid4())
        self.connection = {"backend": "fake", "revision_sha256": "c" * 64}
        self.journal = Journal(Path(self.temp.name) / "journal")
        self.worker = Worker(self.control, {self.connection_id: self.connection}, self.journal,
                             backend_factory=lambda _: self.backend)
        self.source = b"raise RuntimeError('must never execute on the worker')\n"
        request = {
            "schema_version": "1.0", "project_id": str(uuid4()), "connection_id": self.connection_id,
            "name": "test", "command": {"interpreter": "python3", "script": "main.py", "arguments": []},
            "environment": {"id": str(uuid4()), "revision_sha256": "b" * 64},
            "resources": {"nodes": 1, "cpus": 1, "gpus": 0, "memory_mib": 128, "time_limit_seconds": 60},
            "slurm": {"partition": "debug", "account": "broker"},
            "files": [{"path": "main.py", "sha256": hashlib.sha256(self.source).hexdigest(),
                       "size_bytes": len(self.source), "executable": False}], "outputs": [],
        }
        package = prepare(request, {**request["environment"], "interpreter": "/usr/bin/python3"},
                          self.connection, str(uuid4()))
        files = {"main.py": self.source,
                 **{path: text.encode() for path, text in package["generated_files"].items()}}
        self.lease = {"id": str(uuid4()), "operation": "submit", "attempt_id": str(uuid4()),
                      "generation": 1, "connection_id": self.connection_id, "native_identity": None,
                      "prepared": {"manifest": package["manifest"], "manifest_sha256": package["manifest_sha256"],
                                   "files": {path: base64.b64encode(data).decode() for path, data in files.items()}}}

    def test_report_failure_replays_saved_receipt_without_submission(self):
        self.control.fail_report = True
        with self.assertRaises(ControlError):
            self.worker.execute(self.lease)
        self.worker.execute(self.lease)
        self.assertEqual(self.backend.submitted, 1)
        self.assertEqual(self.backend.reconciled, 0)
        self.assertEqual(sum(path.endswith("/dispatch") for _, path, _ in self.control.calls), 1)
        self.assertEqual(self.journal.read(self.lease["attempt_id"])["outcome"], "submitted")

    def test_ambiguous_external_call_is_reconciled_never_retried(self):
        self.backend.failure = True
        self.worker.execute(self.lease)
        self.worker.execute(self.lease)
        self.assertEqual((self.backend.submitted, self.backend.reconciled), (1, 1))
        reports = [body for _, path, body in self.control.calls if path.endswith("/report")]
        self.assertEqual([item["outcome"] for item in reports], ["submission_uncertain"] * 2)
        self.assertNotIn("sensitive", json.dumps(reports))

    def test_persisted_intent_after_crash_requires_reconciliation(self):
        self.journal.write(self.lease["attempt_id"],
                           {"manifest_sha256": self.lease["prepared"]["manifest_sha256"], "outcome": "intent"})
        self.worker.execute(self.lease)
        self.assertEqual((self.backend.submitted, self.backend.reconciled), (0, 1))
        self.assertFalse(any(path.endswith("/dispatch") for _, path, _ in self.control.calls))

    def test_tampered_payload_never_dispatches(self):
        self.lease["prepared"]["files"]["main.py"] = base64.b64encode(b"changed").decode()
        with self.assertRaises(ValueError):
            self.worker.execute(self.lease)
        self.assertEqual(self.control.calls, [])
        self.assertEqual(self.backend.submitted, 0)

    def test_changed_connection_revision_never_dispatches(self):
        self.connection["revision_sha256"] = "d" * 64
        with self.assertRaises(ValueError):
            self.worker.execute(self.lease)
        self.assertEqual(self.control.calls, [])

    def test_terminal_report_includes_bounded_outputs_and_accounting(self):
        self.worker.execute(self.lease)
        self.lease["operation"] = "observe"
        self.lease["native_identity"] = {"job_id": "17"}
        self.worker.execute(self.lease)
        report = self.control.calls[-1][2]
        self.assertEqual(report["outcome"], "observed")
        self.assertTrue(report["observation"]["accounting"]["final"])
        self.assertEqual(report["stdout"], "synthetic result")

    def test_unauthorized_artifact_is_not_reported(self):
        self.lease["operation"] = "observe"
        self.lease["native_identity"] = {"job_id": "17"}
        self.backend.collect = lambda *args, **kwargs: {"artifacts": {"secrets": b"no"}}
        self.worker.execute(self.lease)
        report = self.control.calls[-1][2]
        self.assertTrue(report["observation"]["accounting"]["final"])
        self.assertEqual(report["artifacts"], {})
        self.assertEqual(report["error_code"], "OUTPUT_COLLECTION_FAILED")
        self.assertNotIn("secrets", str(report))

    def test_unavailable_outputs_do_not_suppress_final_accounting(self):
        self.lease["operation"] = "observe"
        self.lease["native_identity"] = {"job_id": "17"}

        def failed_collection(*args, **kwargs):
            raise OSError("sensitive path or remote diagnostic")

        self.backend.collect = failed_collection
        self.worker.execute(self.lease)
        report = self.control.calls[-1][2]
        self.assertEqual(report["outcome"], "observed")
        self.assertEqual(report["observation"]["accounting"]["cpu_core_seconds"], 2)
        self.assertEqual(report["error_code"], "OUTPUT_COLLECTION_FAILED")
        self.assertEqual(report["artifacts"], {})
        self.assertNotIn("sensitive", str(report))


class ClientTests(unittest.TestCase):
    def test_reject_remote_plain_http_and_embedded_credentials(self):
        for url in ("http://remote.example", "https://user:pass@example.com", "https://example.com/path"):
            with self.assertRaises(ValueError):
                JsonClient(url, "synthetic-token")

    def test_strict_response_parser(self):
        for payload in (b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":1.5}',
                        b'{"x":"\\ud800"}', b'{"x":9007199254740992}'):
            with self.assertRaises((ValueError, UnicodeError)):
                strict_json(payload)

    def test_redirect_never_forwards_credential(self):
        seen = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append((self.path, self.headers.get("Authorization")))
                self.send_response(302)
                self.send_header("Location", "/credential-capture")
                self.end_headers()

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = JsonClient(f"http://127.0.0.1:{server.server_port}", "synthetic-token")
            with self.assertRaises(ControlError):
                client.request("GET", "/first")
            self.assertEqual(seen, [("/first", "Bearer synthetic-token")])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


class EnrollmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.profile = {
            "backend": "slurm", "allowed_hosts": ["login.example.invalid"],
            "allowed_users": ["alice"], "allowed_ports": [22],
            "remote_root_prefixes": ["/scratch/alice/broker"], "profile_revision": 1,
            "ssh": {"identity_file": "/protected/id_ed25519", "known_hosts": "/protected/known_hosts", "mode": "key"},
            "python_path": "/usr/bin/python3", "slurm_paths": {"sbatch": "/usr/bin/sbatch"},
            "site_profile": {"partitions": ["debug"], "accounts": ["research"], "qos": [],
                             "termination_margin_seconds": 30, "overtime_margin_seconds": 0,
                             "cleanup_margin_seconds": 30},
            "environment_discovery": {"python_executables": ["/usr/bin/python3"]},
        }
        self.profiles = {"university": self.profile}
        self.public = {"backend": "slurm", "credential_profile": "university",
                       "ssh": {"host": "login.example.invalid", "user": "alice", "port": 22},
                       "remote_root": "/scratch/alice/broker/project"}
        self.control = FakeControl()
        self.backend_calls = []

        def backend_factory(connection):
            self.backend_calls.append(connection)
            return SimpleDiscoveryBackend()

        self.worker = Worker(self.control, {}, Journal(Path(self.temp.name) / "journal"),
                             backend_factory=backend_factory, credential_profiles=self.profiles)

    def discovery_lease(self, configuration=None):
        return {"id": str(uuid4()), "operation": "discover", "connection_id": str(uuid4()),
                "generation": 1, "configuration": configuration or self.public}

    def test_protected_credentials_merge_locally_and_public_revision_is_stable(self):
        private, public, digest = resolve_connection(self.public, self.profiles)
        self.assertEqual(private["ssh"]["identity_file"], "/protected/id_ed25519")
        self.assertEqual(private["ssh"]["host"], "login.example.invalid")
        self.assertEqual(private["environment_discovery"], {"python_executables": ["/usr/bin/python3"]})
        self.assertNotIn("/protected", json.dumps(public))
        self.assertNotIn("identity_file", json.dumps(public))
        again, same, same_digest = resolve_connection(public, self.profiles)
        self.assertEqual((same, same_digest, again), (public, digest, private))
        self.assertEqual(private["revision_sha256"], digest)

    def test_browser_cannot_supply_credential_paths_commands_or_broaden_target(self):
        variants = []
        for key, value in (("host", "attacker.invalid"), ("user", "root"), ("port", 2222)):
            changed = copy.deepcopy(self.public)
            changed["ssh"][key] = value
            variants.append(changed)
        for value in ("/scratch/alice/broker-other", "/scratch/alice/broker/../private", "/etc"):
            changed = copy.deepcopy(self.public)
            changed["remote_root"] = value
            variants.append(changed)
        changed = copy.deepcopy(self.public)
        changed["ssh"]["identity_file"] = "/browser/chosen/key"
        variants.append(changed)
        changed = copy.deepcopy(self.public)
        changed["python_path"] = "/browser/chosen/program"
        variants.append(changed)
        for configuration in variants:
            with self.subTest(configuration=configuration):
                lease = self.discovery_lease(configuration)
                self.worker.execute(lease)
                report = self.control.calls[-1][2]
                self.assertEqual(report["outcome"], "discovery_failed")
                self.assertNotIn("configuration", report)
        self.assertEqual(self.backend_calls, [])

    def test_changed_protected_profile_invalidates_previous_public_binding(self):
        _, old, _ = resolve_connection(self.public, self.profiles)
        self.profile["ssh"]["identity_file"] = "/protected/rotated-key"
        with self.assertRaises(EnrollmentError) as error:
            resolve_connection(old, self.profiles)
        self.assertEqual(error.exception.code, "CREDENTIAL_PROFILE_CHANGED")

    def test_site_automation_review_flag_round_trips_as_protected_boolean_metadata(self):
        digests = []
        for allowed in (False, True):
            with self.subTest(automatic_execution_allowed=allowed):
                self.profile["site_profile"]["automatic_execution_allowed"] = allowed
                metadata = profile_metadata("university", self.profile)
                private, public, digest = resolve_connection(self.public, self.profiles)
                self.assertIs(metadata["site_profile"]["automatic_execution_allowed"], allowed)
                self.assertIs(private["site_profile"]["automatic_execution_allowed"], allowed)
                self.assertIs(public["site_profile"]["automatic_execution_allowed"], allowed)
                self.assertEqual(resolve_connection(public, self.profiles)[2], digest)
                self.worker.publish_profiles()
                self.assertIs(self.control.calls[-1][2]["profiles"][0]["site_profile"]["automatic_execution_allowed"], allowed)
                digests.append(digest)
        self.assertNotEqual(*digests)

    def test_browser_cannot_enable_automation_disabled_by_protected_site_profile(self):
        self.profile["site_profile"]["automatic_execution_allowed"] = False
        _, public, _ = resolve_connection(self.public, self.profiles)
        public["site_profile"]["automatic_execution_allowed"] = True
        with self.assertRaises(EnrollmentError) as denied:
            resolve_connection(public, self.profiles)
        self.assertEqual(denied.exception.code, "CREDENTIAL_PROFILE_CHANGED")

    def test_site_automation_flag_rejects_truthy_numbers_strings_and_other_nonbooleans(self):
        for invalid in (0, 1, "false", "true", None, [], {}):
            with self.subTest(invalid=invalid):
                self.profile["site_profile"]["automatic_execution_allowed"] = invalid
                with self.assertRaises(EnrollmentError) as rejected:
                    profile_metadata("university", self.profile)
                self.assertEqual(rejected.exception.code, "INVALID_CREDENTIAL_PROFILE")

    def test_authorized_discovery_can_renew_derived_binding_but_jobs_remain_strict(self):
        _, old, old_digest = resolve_connection(self.public, self.profiles)
        self.profile["site_profile"]["termination_margin_seconds"] = 60
        with self.assertRaises(EnrollmentError):
            resolve_connection(old, self.profiles)
        self.worker.execute(self.discovery_lease(old))
        report = self.control.calls[-1][2]
        self.assertEqual(report["outcome"], "discovered")
        self.assertNotEqual(report["connection_revision_sha256"], old_digest)
        self.assertEqual(report["configuration"]["site_profile"]["termination_margin_seconds"], 60)
        for key in ("backend", "credential_profile", "ssh", "remote_root"):
            self.assertEqual(report["configuration"][key], old[key])
        # Renewal never bypasses a withdrawn host/user/root permission.
        self.profile["allowed_hosts"] = ["replacement.example.invalid"]
        self.worker.execute(self.discovery_lease(old))
        self.assertEqual(self.control.calls[-1][2]["error_code"], "TARGET_NOT_ALLOWED")

    def test_browser_cannot_replace_trusted_allocation_margins(self):
        _, public, _ = resolve_connection(self.public, self.profiles)
        public["site_profile"]["termination_margin_seconds"] = 0
        with self.assertRaises(EnrollmentError):
            resolve_connection(public, self.profiles)

    def test_discovery_report_and_capability_publication_contain_no_credentials(self):
        self.worker.publish_profiles()
        published = self.control.calls[-1]
        self.assertEqual(published[1], "/internal/v1/worker/profiles")
        metadata = published[2]["profiles"][0]
        self.assertEqual(metadata["alias"], "university")
        self.assertEqual(metadata["allowed_hosts"], ["login.example.invalid"])
        self.assertNotIn("/protected", json.dumps(metadata))
        self.assertNotIn("environment_discovery", metadata)
        self.worker.execute(self.discovery_lease())
        report = self.control.calls[-1][2]
        self.assertEqual(report["outcome"], "discovered")
        self.assertEqual(report["discovery_snapshot"]["partitions"], ["debug"])
        self.assertNotIn("/protected", json.dumps(report))
        self.assertNotIn("slurm_paths", json.dumps(report))
        self.assertEqual(len(self.backend_calls), 1)

    def test_unknown_profile_and_discovery_error_return_bounded_codes(self):
        self.public["credential_profile"] = "missing"
        self.worker.execute(self.discovery_lease())
        self.assertEqual(self.control.calls[-1][2]["error_code"], "CREDENTIAL_PROFILE_UNKNOWN")
        self.assertEqual(self.backend_calls, [])
        self.public["credential_profile"] = "university"

        def failure(config):
            raise OSError("sensitive login command or credential path")

        self.worker.backend_factory = failure
        self.worker.execute(self.discovery_lease())
        report = self.control.calls[-1][2]
        self.assertEqual(report["error_code"], "DISCOVERY_UNAVAILABLE")
        self.assertNotIn("sensitive", json.dumps(report))

    def test_bad_profile_does_not_block_other_connections(self):
        self.profiles["misconfigured"] = {"backend": "slurm", "allowed_hosts": []}
        self.worker.publish_profiles()
        self.assertEqual([item["alias"] for item in self.control.calls[-1][2]["profiles"]], ["university"])

    def test_new_worker_reconstructs_dynamic_job_from_public_lease(self):
        from compute_broker.backends import prepare

        private, public, digest = resolve_connection(self.public, self.profiles)
        source = b"print('cluster payload, never run locally')\n"
        environment = {"id": str(uuid4()), "revision_sha256": "b" * 64, "interpreter": "/usr/bin/python3"}
        connection_id = str(uuid4())
        request = {"schema_version": "1.0", "project_id": str(uuid4()), "connection_id": connection_id,
                   "name": "dynamic", "command": {"interpreter": "python3", "script": "main.py", "arguments": []},
                   "environment": {key: environment[key] for key in ("id", "revision_sha256")},
                   "resources": {"nodes": 1, "cpus": 1, "gpus": 0, "memory_mib": 128, "time_limit_seconds": 60},
                   "slurm": {"partition": "debug", "account": "research"}, "outputs": [],
                   "files": [{"path": "main.py", "sha256": hashlib.sha256(source).hexdigest(),
                              "size_bytes": len(source), "executable": False}]}
        package = prepare(request, environment, private, str(uuid4()))
        self.assertEqual(package["manifest"]["connection_revision_sha256"], digest)
        files = {"main.py": source, **{path: text.encode() for path, text in package["generated_files"].items()}}
        backend = BackendSpy()
        # This worker has no cached/static connection UUID and has not discovered
        # anything in this process. The protected alias supplies credentials.
        worker = Worker(self.control, {}, self.worker.journal, backend_factory=lambda _: backend,
                        credential_profiles=self.profiles)
        worker.execute({"id": str(uuid4()), "operation": "submit", "attempt_id": str(uuid4()),
                        "generation": 1, "connection_id": connection_id, "configuration": public,
                        "prepared": {"manifest": package["manifest"], "manifest_sha256": package["manifest_sha256"],
                                     "files": {path: base64.b64encode(data).decode() for path, data in files.items()}}})
        self.assertEqual(backend.submitted, 1)


class SimpleDiscoveryBackend:
    def discover(self):
        return {"partitions": ["debug"], "simulation": True}


if __name__ == "__main__":
    unittest.main()
