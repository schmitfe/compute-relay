import pytest

@pytest.fixture
def snapshot():
    return {"nodes": [
        {"name": "shared", "partition": "cpu", "cpus": "64", "memory_mib": "256000", "gres": "(null)", "state": "down"},
        {"name": "shared", "partition": "gpu", "cpus": "64", "memory_mib": "256000", "gres": "gpu:a100:4(S:0-1),gpu:rtx:2(S:2)", "state": "allocated"},
        {"name": "large", "partition": "gpu", "cpus": "128", "memory_mib": "512000", "gres": "gpu:a100:8", "state": "idle"}],
        "partitions": [{"PartitionName": "cpu", "MaxTime": "01:00:00", "MaxCPUsPerNode": "32", "MaxMemPerNode": "128000", "AllowAccounts": "ALL", "QoS": "partition-qos"},
            {"PartitionName": "gpu", "MaxTime": "2-00:00:00", "MaxCPUsPerNode": "UNLIMITED", "MaxMemPerNode": "UNLIMITED"}],
        "association_query_status": "available", "associations": [
            {"account": "science", "partition": "", "qos": "normal,high", "default_qos": "normal"},
            {"account": "science", "qos": "normal", "default_qos": ""}],
        "timing_candidates": {"gpu": {"cleanup_margin_seconds": 0, "finite_timing_candidate": True}},
        "scheduler_settings": {"KillWait": "0", "OverTimeLimit": "0"}}

def form_data(**changes):
    return {"partitions": "cpu, gpu", "accounts": "science", "qos": "normal",
        "maximum_cpus": 128, "maximum_gpus": 8, "maximum_memory_mib": 512000,
        "maximum_time_limit_seconds": 172800, **changes}
