"""Bounded Python inventory, executed only as ordinary scheduled compute."""

import hashlib
import json


INVENTORY_PROGRAM = r'''
import hashlib,importlib.metadata,json,platform,sys,sysconfig
def inventory():
    packages=[]
    for index,dist in enumerate(importlib.metadata.distributions()):
        if index>=4096: raise RuntimeError('Inventory exceeds distribution limit')
        name=dist.metadata.get('Name','')
        version=dist.version
        if not name or len(name)>128 or len(version)>128: raise RuntimeError('Invalid distribution metadata')
        packages.append({'name':name,'version':version})
    packages.sort(key=lambda item:(item['name'].lower(),item['version']))
    identity={'language':'python','version':platform.python_version(),'implementation':platform.python_implementation(),
        'platform':sysconfig.get_platform(),'packages':packages}
    fingerprint=hashlib.sha256(json.dumps(identity,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    visible=[]
    for package in packages:
        if len(json.dumps(visible+[package],separators=(',',':')).encode())>24000: break
        visible.append(package)
    return {**identity,'packages':visible,'package_count':len(packages),'truncated':len(visible)!=len(packages),
        'fingerprint':fingerprint,'executable':sys.executable,'prefix':sys.prefix}
'''


INSPECTION_SCRIPT = INVENTORY_PROGRAM + r'''
import datetime,pathlib,uuid
root=pathlib.Path.cwd()
configuration=json.loads((root/'_environment/inspection.json').read_text())
result={'schema_version':'1.0','inspection_id':configuration['inspection_id'],
    'environment':configuration['environment'],'attempt_id':str(uuid.UUID(root.name)),
    'observed_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'scope':'compute_node','runtime':inventory()}
payload=json.dumps(result,sort_keys=True,separators=(',',':')).encode()
if len(payload)>32768: raise RuntimeError('Inventory receipt exceeds limit')
(root/'_environment/inventory.json').write_bytes(payload)
print('ENVIRONMENT_INVENTORY_COLLECTED')
'''


def validate_runtime(runtime):
    import re
    if not isinstance(runtime, dict) or runtime.get("language") != "python" or not isinstance(runtime.get("packages"), list):
        raise ValueError("Invalid runtime inventory")
    if len(json.dumps(runtime).encode()) > 30000 or type(runtime.get("package_count")) is not int or type(runtime.get("truncated")) is not bool:
        raise ValueError("Invalid inventory bounds")
    for key in ("version", "implementation", "platform", "executable", "prefix", "fingerprint"):
        if not isinstance(runtime.get(key), str) or len(runtime[key]) > 4096:
            raise ValueError("Invalid runtime identity")
    if not re.fullmatch(r"[0-9a-f]{64}", runtime["fingerprint"]):
        raise ValueError("Invalid inventory digest")
    for package in runtime["packages"]:
        if not isinstance(package, dict) or set(package) != {"name", "version"} or any(not isinstance(value, str) or len(value) > 128 for value in package.values()):
            raise ValueError("Invalid package inventory")
    if runtime["package_count"] < len(runtime["packages"]):
        raise ValueError("Invalid package count")
    if not runtime["truncated"]:
        identity = {key: runtime[key] for key in ("language", "version", "implementation", "platform", "packages")}
        digest = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        if digest != runtime["fingerprint"] or runtime["package_count"] != len(runtime["packages"]):
            raise ValueError("Inventory fingerprint mismatch")
    return runtime
