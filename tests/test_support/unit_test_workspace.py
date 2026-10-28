import json

TOKEN = "cbp_" + "synthetic_workspace_only_" * 2

def workspace(path, url="http://127.0.0.1:8768", token=TOKEN):
    path.mkdir(exist_ok=True)
    private = path / ".compute-relay"
    private.mkdir(mode=0o700)
    (private / "project.key").write_text(token)
    (private / "project.key").chmod(0o600)
    (private / "connection.json").write_text(json.dumps({"schema_version": "1.0", "broker_url": url,
        "workspace_root": str(path)}))
    (private / "connection.json").chmod(0o600)
    return path
