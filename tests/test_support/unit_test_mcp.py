from uuid import uuid4

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
