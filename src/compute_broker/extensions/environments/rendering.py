"""Pure validation/rendering. Package installation occurs only in scheduled jobs."""

import hashlib
import json
import re

SUPPORTED_MANAGERS = ("venv",)
_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,79}$")
_DIGEST = re.compile(r"^[a-f0-9]{64}$")


def integer(value, label, maximum, minimum=0):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("Invalid " + label)
    return value


def validate_policy(document):
    required = {"enabled", "allowed_base_environment_ids", "allowed_managers", "allowed_sources",
                "resource_limits", "platform_tag", "python_major_minor", "max_environment_bytes", "max_environment_files"}
    optional = {"minimum_free_bytes", "minimum_free_files"}
    if not isinstance(document, dict) or not required <= set(document) or set(document) - required - optional:
        raise ValueError("Invalid environment build policy fields")
    if type(document["enabled"]) is not bool:
        raise ValueError("enabled must be boolean")
    from uuid import UUID
    bases = document["allowed_base_environment_ids"]
    if not isinstance(bases, list) or len(bases) > 100 or any(not isinstance(value, str) for value in bases) or len(set(bases)) != len(bases):
        raise ValueError("Invalid base environment list")
    for value in bases:
        if str(UUID(value)) != value:
            raise ValueError("Expected canonical base environment UUID")
    if document["allowed_managers"] != ["venv"] or document["allowed_sources"] != ["uploaded_wheels"]:
        raise ValueError("This version supports only venv with uploaded wheels; other managers are unavailable")
    limits = document["resource_limits"]
    if not isinstance(limits, dict) or set(limits) != {"cpus", "memory_mib", "time_limit_seconds"}:
        raise ValueError("Invalid build resource limits")
    integer(limits["cpus"], "CPU limit", 1024, 1)
    integer(limits["memory_mib"], "memory limit", 16777216, 128)
    integer(limits["time_limit_seconds"], "time limit", 604800, 60)
    if limits["time_limit_seconds"] % 60:
        raise ValueError("Time limits use whole minutes")
    if not isinstance(document["platform_tag"], str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", document["platform_tag"]):
        raise ValueError("Pin the compute platform tag")
    if not isinstance(document["python_major_minor"], str) or not re.fullmatch(r"3\.[0-9]{1,2}", document["python_major_minor"]):
        raise ValueError("Pin Python major.minor")
    for name in ("max_environment_bytes", "minimum_free_bytes"):
        if name in document:
            integer(document[name], name, 2**53 - 1)
    for name in ("max_environment_files", "minimum_free_files"):
        if name in document:
            integer(document[name], name, 10000000)
    return document


def normalize_request(document, policy):
    allowed = {"name", "connection_id", "base_environment", "manager", "requirements", "resources", "slurm", "estimated_bytes", "estimated_files"}
    if not isinstance(document, dict) or set(document) != allowed:
        raise ValueError("Invalid environment build request fields")
    if not isinstance(document["name"], str) or not _NAME.fullmatch(document["name"]):
        raise ValueError("Invalid environment name")
    if document["manager"] != "venv":
        raise ValueError("Unsupported environment manager; only venv is implemented")
    rows = document["requirements"]
    if not isinstance(rows, list) or len(rows) > 64:
        raise ValueError("At most 64 uploaded wheels are supported")
    requirements = []
    names, filenames = set(), set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"name", "version", "filename", "sha256", "size_bytes"}:
            raise ValueError("Invalid pinned wheel descriptor")
        name, version, filename = row["name"], row["version"], row["filename"]
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", name):
            raise ValueError("Invalid distribution name")
        normalized = re.sub(r"[-_.]+", "-", name).lower()
        if not isinstance(version, str) or not re.fullmatch(r"[0-9][A-Za-z0-9_.+!]{0,79}", version):
            raise ValueError("A concrete pinned version is required")
        if not isinstance(filename, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.+!-]{0,179}\.whl", filename):
            raise ValueError("Only regular uploaded wheel filenames are accepted")
        parts = filename[:-4].split("-")
        if len(parts) not in {5, 6} or re.sub(r"[-_.]+", "-", parts[0]).lower() != normalized or parts[1] != version:
            raise ValueError("Wheel filename must match its exact name/version")
        if normalized in names or filename in filenames:
            raise ValueError("Duplicate distribution or wheel")
        if not isinstance(row["sha256"], str) or not _DIGEST.fullmatch(row["sha256"]):
            raise ValueError("Every wheel needs SHA-256")
        integer(row["size_bytes"], "wheel size", 104857600, 1)
        names.add(normalized)
        filenames.add(filename)
        requirements.append({**row, "name": normalized})
    if sum(row["size_bytes"] for row in rows) > 100 * 1024 * 1024:
        raise ValueError("Wheel closure exceeds package limit")
    resources = document["resources"]
    if not isinstance(resources, dict) or set(resources) != {"nodes", "cpus", "gpus", "memory_mib", "time_limit_seconds"}:
        raise ValueError("Builds need explicit single-node CPU resources")
    if resources["nodes"] != 1 or type(resources["nodes"]) is not int or type(resources["gpus"]) is not int or resources["gpus"] != 0:
        raise ValueError("Environment builds currently use one CPU-only node")
    for key, minimum in (("cpus", 1), ("memory_mib", 128), ("time_limit_seconds", 60)):
        integer(resources[key], key, policy["resource_limits"][key], minimum)
    if resources["time_limit_seconds"] % 60:
        raise ValueError("Build walltime must use whole minutes")
    integer(document["estimated_bytes"], "estimated bytes", policy["max_environment_bytes"])
    integer(document["estimated_files"], "estimated files", policy["max_environment_files"])
    return {**document, "requirements": sorted(requirements, key=lambda row: row["name"]),
            "platform_tag": policy["platform_tag"], "python_major_minor": policy["python_major_minor"]}


def requirements_text(document):
    return "".join(f"{row['name']}=={row['version']} --hash=sha256:{row['sha256']}\n" for row in document["requirements"])


BUILD_SCRIPT = r'''"""Approved scheduled environment build; never run in control/worker/login."""
import datetime,hashlib,json,os,pathlib,stat,subprocess,sys,sysconfig,uuid,venv
root=pathlib.Path.cwd()
attempt_id=str(uuid.UUID(root.name))
recipe=json.loads((root/'_environment/recipe.json').read_text())
policy=recipe['policy']
if sysconfig.get_platform()!=recipe['platform_tag'] or '.'.join(map(str,sys.version_info[:2]))!=recipe['python_major_minor']:
    raise RuntimeError('Build node Python/platform does not match the approved target')
capacity=os.statvfs(root)
if capacity.f_bavail*capacity.f_frsize < policy.get('minimum_free_bytes',0)+recipe['estimated_bytes']:
    raise RuntimeError('Insufficient observed filesystem byte headroom')
if policy.get('minimum_free_files',0)+recipe['estimated_files'] and (capacity.f_files==0 or capacity.f_favail < policy.get('minimum_free_files',0)+recipe['estimated_files']):
    raise RuntimeError('Insufficient or unknown observed inode headroom')
prefix=root/'environment'
if prefix.exists() or prefix.is_symlink(): raise RuntimeError('Environment prefix already exists; use a fresh build attempt')
os.umask(0o077)
venv.EnvBuilder(with_pip=bool(recipe['requirements']),symlinks=False).create(prefix)
python=str(prefix/'bin/python')
env={'PATH':str(prefix/'bin')+':/usr/bin:/bin','HOME':str(root),'LANG':'C','LC_ALL':'C','PYTHONNOUSERSITE':'1',
     'PIP_CONFIG_FILE':'/dev/null','PIP_DISABLE_PIP_VERSION_CHECK':'1','PIP_NO_INPUT':'1','PIP_NO_CACHE_DIR':'1'}
if recipe['requirements']:
    subprocess.run([python,'-I','-m','pip','install','--no-index','--no-deps','--only-binary=:all:',
        '--require-hashes','--find-links',str(root/'_environment/wheels'),'-r',str(root/'_environment/requirements.lock')],env=env,check=True)
    subprocess.run([python,'-I','-m','pip','check'],env=env,check=True)
probe=subprocess.run([python,'-I','-c','import json,sys,sysconfig; print(json.dumps({"version":sys.version.split()[0],"platform":sysconfig.get_platform()}))'],
    env=env,check=True,capture_output=True,text=True,timeout=30)
if len(probe.stdout)>4096: raise RuntimeError('Validation output exceeded limit')
observed=json.loads(probe.stdout)
if observed['platform']!=recipe['platform_tag'] or '.'.join(observed['version'].split('.')[:2])!=recipe['python_major_minor']:
    raise RuntimeError('Created interpreter failed target validation')
size=0
count=1
for directory,dirs,files in os.walk(prefix,followlinks=False):
    for name in dirs+files:
        info=os.lstat(os.path.join(directory,name))
        count+=1
        if stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode): size+=info.st_size
        if size>policy['max_environment_bytes'] or count>policy['max_environment_files']:
            raise RuntimeError('Built environment exceeds approved bytes/files envelope')
receipt={'schema_version':'1.0','build_id':recipe['build_id'],'spec_sha256':recipe['spec_sha256'],'attempt_id':attempt_id,
    'manager':'venv','prefix_relative':'environment','interpreter_relative':'environment/bin/python',
    'python_version':observed['version'],'python_major_minor':recipe['python_major_minor'],'platform_tag':recipe['platform_tag'],
    'validated':True,'measured_bytes':size,'measured_files':count,'measured_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),
    'requirements_sha256':hashlib.sha256((root/'_environment/requirements.lock').read_bytes()).hexdigest()}
temporary=root/'_environment/receipt.tmp'
with temporary.open('x') as stream:
    json.dump(receipt,stream,sort_keys=True,separators=(',',':'));stream.flush();os.fsync(stream.fileno())
temporary.replace(root/'_environment/receipt.json')
print('ENVIRONMENT_BUILD_VALIDATED')
'''


def render_build_files(document, *, build_id, spec_sha256, policy):
    lock = requirements_text(document)
    recipe = {**document, "build_id": str(build_id), "spec_sha256": spec_sha256, "policy": policy}
    return {"_environment/build.py": BUILD_SCRIPT.encode(),
            "_environment/recipe.json": json.dumps(recipe, sort_keys=True, separators=(",", ":")).encode(),
            "_environment/requirements.lock": lock.encode()}
