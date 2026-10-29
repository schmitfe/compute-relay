"""One GPU permission check for preparation, admission, dispatch and planning."""

from compute_relay.domain.cluster_configuration import gpu_type_inventory


def untyped_gpu_partitions(policy, connection):
    if policy.get("allow_untyped_gpus") is not True or not policy["allowed_gpu_types"]:
        return []
    allowed = set(policy["allowed_gpu_types"])
    result = []
    for partition in connection.configuration.get("site_profile", {}).get("partitions", []):
        inventory = gpu_type_inventory(connection.discovery_snapshot, [partition])
        if inventory["complete"] and inventory["types"] and set(inventory["types"]) <= allowed:
            result.append(partition)
    return result


def gpu_request_allowed(policy, connection, request, allocation):
    if not allocation["gpu_devices"]:
        return True
    resources = request["resources"]
    if not resources["gpus"]:
        return False
    if resources.get("gpu_type"):
        return resources["gpu_type"] in policy["allowed_gpu_types"]
    return request.get("slurm", {}).get("partition") in untyped_gpu_partitions(policy, connection)
