import asyncio
import hashlib
import json
import subprocess
import sys
from uuid import uuid4
import pytest
from compute_relay.backends import BackendError, prepare

def digest(value):
    return hashlib.sha256(value).hexdigest()


class LocalFixedTransport:
    def __init__(self):
        self.operations = []

    async def call_helper(self, source, request):
        self.operations.append(request["operation"])
        process = await asyncio.create_subprocess_exec(sys.executable, "-I", "-c", source,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, _ = await asyncio.wait_for(process.communicate(json.dumps(request).encode()), timeout=10)
        if process.returncode:
            raise BackendError("Fixed fixture helper rejected request")
        return json.loads(stdout)

@pytest.fixture
def site(tmp_path):
    state = tmp_path / "scheduler.json"
    calls = tmp_path / "calls"
    program = tmp_path / "scheduler"
    # A fixed scheduler fixture records accepted jobs but never starts uploaded
    # scripts. Its queue/accounting identity is generated from submitted marker.
    program.write_text(f'''#!{sys.executable}
import datetime,json,sys
from pathlib import Path
state=Path({str(state)!r}); calls=Path({str(calls)!r})
command=Path(sys.argv[0]).name
with calls.open('a') as stream: stream.write(command+'\\n')
data=json.loads(state.read_text()) if state.exists() else {{}}
if command=='sbatch':
    data.update(marker=next(item.split('=',1)[1] for item in sys.argv[1:] if item.startswith('--comment=')),
        submitted=datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S'),state='RUNNING')
    state.write_text(json.dumps(data))
    if data.get('lose_reply'): sys.exit(1)
    print('42;fixture')
elif command=='squeue':
    if data.get('state')=='RUNNING':
        fmt=sys.argv[sys.argv.index('-o')+1]
        print(('42|RUNNING|' if '%T' in fmt else '42|')+data['marker']+'|'+data['submitted'])
elif command=='scancel':
    data['state']='CANCELLED'; state.write_text(json.dumps(data))
elif command=='sacct':
    if data.get('state')=='CANCELLED':
        print('42|CANCELLED|1|1|cpu=1|0:0|'+data['submitted']+'|'+data['submitted']+'|'+data['submitted']+'|'+data['marker']+'|'+data.get('work_dir','')+'|')
''')
    program.chmod(0o700)
    paths = {}
    for name in ("sbatch", "squeue", "sacct", "scancel"):
        path = tmp_path / name
        path.symlink_to(program)
        paths[name] = str(path)
    connection = {"backend": "slurm", "revision_sha256": "f" * 64, "remote_root": str(tmp_path / "jobs"), "slurm_paths": paths,
                  "site_profile": {"partitions": ["debug"]}}
    script = b"raise RuntimeError('submitted code must stay inert in this fixture')\n"
    request = {"schema_version": "1.0", "project_id": str(uuid4()), "connection_id": str(uuid4()), "name": "upgrade-test",
               "command": {"interpreter": "python3", "script": "main.py", "arguments": []},
               "environment": {"id": str(uuid4()), "revision_sha256": "a" * 64},
               "resources": {"nodes": 1, "cpus": 1, "gpus": 0, "memory_mib": 128, "time_limit_seconds": 60},
               "slurm": {"partition": "debug"}, "outputs": ["result.txt"],
               "files": [{"path": "main.py", "sha256": digest(script), "size_bytes": len(script), "executable": False}]}
    return connection, request, script, state, calls

def prepared(site):
    connection, request, script, *_ = site
    value = prepare(request, {**request["environment"], "interpreter": "/usr/bin/python3"}, connection, str(uuid4()))
    files = {"main.py": script, **{path: text.encode() for path, text in value["generated_files"].items()}}
    return value, files
