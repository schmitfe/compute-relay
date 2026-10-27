"""Small read-only scheduler snapshot; no allocations or software discovery."""

from .scheduler import scheduler


def availability(config, request):
    result = {}
    for name, command, arguments in (
        ("nodes", "sinfo", ["-N", "-h", "-O", "NodeHost:256|,Partition:80|,StateLong:80|,CPUs:80|,Gres:1024|,GresUsed:1024"]),
        # Slurm warns against combining CPUsState with node state fields: it
        # splits CPU states across records. Join this independent view by node.
        ("cpus", "sinfo", ["-N", "-h", "-O", "NodeHost:256|,Partition:80|,CPUsState:80"]),
        ("queue", "squeue", ["-h", "-O", "Partition:80|,State:40|,tres-per-job:512|,tres-per-node:512|,tres-per-socket:512|,tres-per-task:512|,Reason:256"]),
    ):
        try:
            value = scheduler(config, command, arguments, timeout=5)
            value["stdout"] = value["stdout"][:512 * 1024]
            value["truncated"] = len(value["stdout"]) >= 512 * 1024
            result[name] = value
        except Exception:
            result[name] = {"returncode": -1, "stdout": "", "truncated": False}
    return result
