"""Deterministic assembly of installed, reviewed stdlib-only remote programs.

Only the fixed component lists below are read. Neither a request, a manifest nor
a remote journal can select a source file or provide executable helper source.
Components share one namespace in the isolated one-shot interpreter, which keeps
the remote deployment independent of a package installation on the cluster.
"""

from importlib.resources import files

from .protocol import JOB_PROTOCOL


DISCOVERY_PROTOCOL = "slurm-discovery/v1"
COMMON_COMPONENTS = ("filesystem.py", "scheduler.py")
JOB_COMPONENTS = ("protocol.py", "jobs.py", "collection.py", "job_dispatch.py")
DISCOVERY_COMPONENTS = (
    "discovery_config.py", "module_catalog.py", "software.py", "discovery.py",
)
ENTRYPOINT = "entrypoint.py"


def _component(name: str) -> str:
    # Explicit newlines and no build paths, timestamps or platform metadata.
    return files("compute_relay.remote").joinpath(name).read_text(encoding="utf-8").replace("\r\n", "\n").rstrip() + "\n"


def component_names(kind: str) -> tuple[str, ...]:
    if kind == "jobs":
        return (*COMMON_COMPONENTS, *JOB_COMPONENTS, ENTRYPOINT)
    if kind == "discovery":
        return (*COMMON_COMPONENTS, *DISCOVERY_COMPONENTS, ENTRYPOINT)
    raise ValueError("Unsupported fixed remote bundle")


def _assemble(names: tuple[str, ...], dispatch: str, protocol: str) -> str:
    source = "# Compute Relay fixed remote bundle; generated deterministically.\n"
    source += "BUNDLE_PROTOCOL = " + repr(protocol) + "\n"
    for name in names[:-1]:
        source += "\n# Component: " + name + "\n" + _component(name)
    source += "\n" + dispatch + "\n# Component: " + ENTRYPOINT + "\n" + _component(ENTRYPOINT)
    return source


def bundle_source(kind: str) -> str:
    names = component_names(kind)
    if kind == "jobs":
        dispatch = "def execute(config, request):\n    return execute_jobs(config, request)\n"
        return _assemble(names, dispatch, JOB_PROTOCOL)
    dispatch = ("def execute(config, request):\n"
                "    if request.get('operation') != 'discover':\n"
                "        raise ValueError('Unsupported fixed discovery operation')\n"
                "    return discover(config, request)\n")
    return _assemble(names, dispatch, DISCOVERY_PROTOCOL)


def local_inspection_source() -> str:
    """The same reviewed components together for in-process helper tests.

    This local facade is never selected or sent by a remote operation. It lets
    hostile-input tests patch a single namespace, identical to the one-shot
    interpreter, without importing any job, cluster or archived helper source.
    """
    names = (*COMMON_COMPONENTS, *JOB_COMPONENTS, *DISCOVERY_COMPONENTS, ENTRYPOINT)
    dispatch = ("def execute(config, request):\n"
                "    if request.get('operation') == 'discover':\n"
                "        return discover(config, request)\n"
                "    return execute_jobs(config, request)\n")
    return _assemble(names, dispatch, JOB_PROTOCOL)
