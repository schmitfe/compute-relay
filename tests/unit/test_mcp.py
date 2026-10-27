import asyncio
import base64
import unittest
from uuid import uuid4

from mcp.server.fastmcp.exceptions import ToolError

from compute_relay.mcp.facade import ProjectFacade, create_server


class ClientSpy:
    def __init__(self):
        self.calls = []
        self.job_id = str(uuid4())
        self.project_id = str(uuid4())

    def request(self, method, path, body=None, **kwargs):
        self.calls.append((method, path, body, kwargs))
        if path == "/api/v1/context":
            return {"schema_version": "1.0", "project": {"id": self.project_id, "name": "synthetic"}}
        return {"id": self.job_id}

    def get_bytes(self, path, limit):
        self.calls.append(("GET_BYTES", path, None, {"limit": limit}))
        return b"<script>untrusted</script>", "text/html"


class FacadeTests(unittest.TestCase):
    def setUp(self):
        self.client = ClientSpy()
        self.project_id = self.client.project_id
        self.facade = ProjectFacade(self.client, self.project_id)

    def test_official_sdk_exposes_only_project_tools(self):
        server = create_server(self.facade)
        tools = asyncio.run(server.list_tools())
        names = {tool.name for tool in tools}
        self.assertEqual(names, {"get_project_context", "get_broker_capabilities", "list_compute_targets", "get_project_allowance", "upload_job_file",
                                 "prepare_job", "revise_job", "request_execution", "get_job", "get_job_logs",
                                 "list_job_artifacts", "get_job_artifact", "retry_job_outputs", "cancel_job_attempt", "cancel_execution_request",
                                 "list_project_jobs", "acknowledge_job_results", "get_compute_availability", "cancel_prepared_job"})
        self.assertTrue(next(t for t in tools if t.name == "get_job").annotations.readOnlyHint)

    def test_project_is_resolved_from_authenticated_context(self):
        facade = ProjectFacade.from_credential(self.client)
        self.assertEqual(facade.project_id, self.project_id)
        self.assertEqual(self.client.calls, [("GET", "/api/v1/context", None, {})])

    def test_context_is_refreshed_and_cannot_rebind_the_adapter(self):
        self.assertEqual(self.facade.get_project_context()["project"]["id"], self.project_id)
        self.client.project_id = str(uuid4())
        with self.assertRaises(ValueError):
            self.facade.get_project_context()

    def test_prepare_and_revision_insert_project_without_mutating_inputs(self):
        request = {"name": "synthetic"}
        self.facade.prepare_job(request, "create")
        self.facade.revise_job(self.client.job_id, request, "revision")
        self.assertEqual(request, {"name": "synthetic"})
        for index in (0, 2):
            self.assertEqual(self.client.calls[index][2], {**request, "project_id": self.project_id})

    def test_workflow_node_requests_infer_project_and_reject_other_projects(self):
        server = create_server(ProjectFacade(self.client, self.project_id, enabled_extensions=["workflows"]))
        document = {"name": "synthetic", "nodes": [{"key": "first", "request": {"name": "first"}}]}
        asyncio.run(server.call_tool("prepare_workflow", {"document": document}))
        self.assertNotIn("project_id", document["nodes"][0]["request"])
        self.assertEqual(self.client.calls[-1][2]["nodes"][0]["request"]["project_id"], self.project_id)
        count = len(self.client.calls)
        document["nodes"][0]["request"]["project_id"] = str(uuid4())
        with self.assertRaisesRegex(ToolError, "API key's project"):
            asyncio.run(server.call_tool("prepare_workflow", {"document": document}))
        self.assertEqual(len(self.client.calls), count)

    def test_project_override_is_rejected_before_http(self):
        with self.assertRaises(ValueError):
            self.facade.prepare_job({"project_id": str(uuid4())})
        self.assertEqual(self.client.calls, [])

    def test_create_and_prepare_have_stable_separate_idempotency_keys(self):
        self.facade.prepare_job({"project_id": self.project_id}, "retry-key")
        self.assertEqual(self.client.calls[0][3]["idempotency_key"], "retry-key")
        self.assertEqual(self.client.calls[1][3]["idempotency_key"], "retry-key:prepare")

    def test_invalid_job_path_cannot_inject_an_admin_endpoint(self):
        with self.assertRaises(ValueError):
            self.facade.request_execution("../../admin")
        self.assertEqual(self.client.calls, [])

    def test_revision_keeps_project_and_idempotency_boundaries(self):
        job_id = str(uuid4())
        self.facade.revise_job(job_id, {"project_id": self.project_id}, "revision-1")
        self.assertEqual(self.client.calls[0][1], f"/api/v1/jobs/{job_id}/revisions")
        self.assertEqual(self.client.calls[1][3]["idempotency_key"], "revision-1:prepare")
        with self.assertRaises(ValueError):
            self.facade.revise_job(job_id, {"project_id": str(uuid4())})

    def test_upload_uses_content_and_never_opens_local_path(self):
        encoded = base64.b64encode(b"print('source')").decode()
        self.facade.upload_job_file("main.py", encoded)
        self.assertEqual(self.client.calls[-1][2], {"path": "main.py", "content_base64": encoded})

    def test_artifacts_are_data_instead_of_active_markup(self):
        result = self.facade.get_job_artifact(str(uuid4()))
        self.assertEqual(base64.b64decode(result["content_base64"]), b"<script>untrusted</script>")
        self.assertNotIn("content", result)

    def test_log_request_is_bounded(self):
        with self.assertRaises(ValueError):
            self.facade.get_job_logs(str(uuid4()), limit=100_000_000)
        self.assertEqual(self.client.calls, [])


if __name__ == "__main__":
    unittest.main()
