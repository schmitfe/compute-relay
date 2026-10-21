"""Pure bounded graph validation, with no expressions or remote operations."""

import re

from compute_broker.domain.validation import relative_path, validate

MAX_NODES = 32


def validate_workflow(document):
    if not isinstance(document, dict) or set(document) != {"name", "nodes"}:
        raise ValueError("Workflow requires a name and nodes")
    pattern = r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,79}"
    if not isinstance(document["name"], str) or not re.fullmatch(pattern, document["name"]):
        raise ValueError("Invalid workflow name")
    nodes = document["nodes"]
    if not isinstance(nodes, list) or not 1 <= len(nodes) <= MAX_NODES:
        raise ValueError("Workflow must have between one and 32 nodes")
    by_key = {}
    for node in nodes:
        if not isinstance(node, dict) or set(node) - {"key", "request", "inputs", "depends_on"} or not {"key", "request"} <= node.keys():
            raise ValueError("Invalid workflow node")
        key = node["key"]
        if not isinstance(key, str) or not re.fullmatch(pattern, key) or key in by_key:
            raise ValueError("Node keys must be unique names")
        validate("job-request.schema.json", node["request"])
        by_key[key] = node
    dependencies = {}
    for node in nodes:
        declared = node.get("depends_on", [])
        inputs = node.get("inputs", [])
        if not isinstance(declared, list) or len(declared) > MAX_NODES or any(not isinstance(key, str) for key in declared) or len(set(declared)) != len(declared):
            raise ValueError("Invalid dependencies")
        if not isinstance(inputs, list) or len(inputs) > 100:
            raise ValueError("Invalid artifact edges")
        edges = set(declared)
        paths = {item["path"] for item in node["request"]["files"]}
        for item in inputs:
            if not isinstance(item, dict) or set(item) != {"node", "output", "path"} or not isinstance(item["node"], str):
                raise ValueError("Invalid artifact edge")
            producer = by_key.get(item["node"])
            if producer is None or item["output"] not in producer["request"]["outputs"]:
                raise ValueError("Producer must declare the requested output")
            path = relative_path(item["path"])
            if path.startswith("_broker/") or path == node["request"]["command"]["script"] or any(
                    path == old or path.startswith(old + "/") or old.startswith(path + "/") for old in paths):
                raise ValueError("Artifact inputs cannot replace code or conflict with other files")
            paths.add(path)
            edges.add(item["node"])
        if len(paths) > 100 or any(key not in by_key or key == node["key"] for key in edges):
            raise ValueError("Invalid graph dependency or file count")
        dependencies[node["key"]] = edges
    pending = {key: set(value) for key, value in dependencies.items()}
    ordered = []
    while pending:
        ready = sorted(key for key, values in pending.items() if not values)
        if not ready:
            raise ValueError("Workflow graph contains a cycle")
        for key in ready:
            ordered.append(key)
            pending.pop(key)
        for values in pending.values():
            values.difference_update(ready)
    return by_key, dependencies, ordered
