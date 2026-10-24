"""Reviewed standard-library source component for the fixed one-shot bundle.

This component is assembled with explicit dependencies by bundles.py. It is not
a remotely selected module or an import of submitted/project code.
"""

import subprocess

def scheduler(config, command, args, *, cwd=None, timeout=30):
    path = config.get("slurm_paths", {}).get(command, "/usr/bin/" + command)
    if not isinstance(path, str) or not re.fullmatch(r"/[A-Za-z0-9_./-]+", path):
        raise ValueError("Scheduler executable must be an approved absolute path")
    env = {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C", "TZ": "UTC"}
    # Configured cluster-specific values are admin-controlled, never agent data.
    extra = config.get("scheduler_environment", {})
    if not isinstance(extra, dict) or any(not NAME.fullmatch(k) or k.startswith("SBATCH_") or not isinstance(v, str) for k, v in extra.items()):
        raise ValueError("Invalid scheduler environment")
    env.update(extra)
    result = subprocess.run([path, *args], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=timeout, cwd=cwd, env=env, check=False)
    if len(result.stdout) > 2 * 1024 * 1024 or len(result.stderr) > 65536:
        raise ValueError("Scheduler output exceeds helper limits")
    return {"returncode": result.returncode, "stdout": result.stdout.decode("utf-8", "replace"),
            "stderr": result.stderr.decode("utf-8", "replace")}
