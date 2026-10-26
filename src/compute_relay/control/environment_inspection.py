"""Small, cluster-bound environment observations for the human recipe form."""

import re

from compute_relay.backends.environments import absolute_environment_path


def validate_result(result):
    allowed = {"ok", "scope", "verified_on_compute", "python", "conda_executable", "poetry_executable",
               "environments", "conda_status", "truncated"}
    if (not isinstance(result, dict) or set(result) != allowed or result["ok"] is not True
            or result["scope"] != "login_node" or result["verified_on_compute"] is not False
            or type(result["truncated"]) is not bool or result["conda_status"] not in {"available", "unavailable", "not_found"}):
        raise ValueError("Invalid environment observation")
    for key in ("conda_executable", "poetry_executable"):
        if not isinstance(result[key], str):
            raise ValueError("Invalid executable")
        if result[key]:
            absolute_environment_path(result[key])
            expected = "conda" if key == "conda_executable" else "poetry"
            if result[key].rsplit("/", 1)[-1] != expected:
                raise ValueError("Invalid manager executable")
    if not isinstance(result["python"], list) or len(result["python"]) > 16:
        raise ValueError("Too many Python paths")
    for path in result["python"]:
        absolute_environment_path(path)
        if not re.fullmatch(r"python(?:3(?:\.[0-9]+)?)?", path.rsplit("/", 1)[-1]):
            raise ValueError("Invalid Python path")
    if not isinstance(result["environments"], list) or len(result["environments"]) > 128:
        raise ValueError("Too many environment prefixes")
    for item in result["environments"]:
        if not isinstance(item, dict) or set(item) != {"kind", "prefix", "interpreter"} or item["kind"] not in {"venv", "conda", "poetry"}:
            raise ValueError("Invalid environment prefix")
        absolute_environment_path(item["prefix"])
        if item["interpreter"] != item["prefix"] + "/bin/python":
            raise ValueError("Invalid prefix interpreter")


def selection(data):
    """Only validated fields; no shell text, SSH targets or search programs."""
    value = {"modules": data["module_names"]}
    for key in ("module_init", "conda_executable", "prefix"):
        if data.get(key):
            value[key] = data[key]
    if data.get("interpreter", "").startswith("/") and data["interpreter"].rsplit("/", 1)[-1] != "bash":
        value["interpreter"] = data["interpreter"]
    if data["module_path_list"]:
        value["module_paths"] = data["module_path_list"]
    return value


def module_selection(value):
    return {key: value.get(key) for key in ("modules", "module_init", "module_paths")}
