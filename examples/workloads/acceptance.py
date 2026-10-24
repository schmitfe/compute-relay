"""Tiny stdlib-only compute/output probe for an explicitly authorized site test.

No environment installation, network requests, datasets or nested jobs are used.
Run only in a reviewed scheduler allocation. Optional first argument is a marker.
"""

import hashlib
import json
import os
from pathlib import Path
import platform
import socket
import sys


marker = sys.argv[1] if len(sys.argv) == 2 else "compute-relay-acceptance"
payload = bytes(range(256)) * 256
result = {
    "marker": marker,
    "sum_1_to_10000": sum(range(1, 10001)),
    "payload_sha256": hashlib.sha256(payload).hexdigest(),
    "hostname": socket.gethostname(),
    "python": platform.python_version(),
    "slurm_job_id": os.environ.get("SLURM_JOB_ID"),
    "slurm_cpus_per_task": os.environ.get("SLURM_CPUS_PER_TASK"),
}
Path("result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
print("COMPUTE_BROKER_ACCEPTANCE_OK")
print(json.dumps(result, sort_keys=True))
