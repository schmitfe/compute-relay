"""Reviewed standard-library source component for the fixed one-shot bundle.

This component is assembled with explicit dependencies by bundles.py. It is not
a remotely selected module or an import of submitted/project code.
"""

import pwd

def discover(config, request):
    def query(command, args):
        try:
            return scheduler(config, command, args)
        except Exception as exc:
            return {"returncode": -1, "stdout": "", "stderr": type(exc).__name__}

    settings = query("scontrol", ["show", "config"])
    # Return only relevant scheduler facts, not the entire server configuration.
    setting_names = {"KillWait", "OverTimeLimit", "JobRequeue", "PrologFlags", "SelectType", "SelectTypeParameters", "PreemptMode", "AccountingStoreFlags"}
    settings["stdout"] = "\n".join(line for line in settings["stdout"].splitlines()
                                     if line.split("=", 1)[0].strip() in setting_names)
    user = pwd.getpwuid(os.getuid()).pw_name
    return {"version": query("sinfo", ["--version"]),
            "nodes": query("sinfo", ["-N", "-h", "-o", "%N|%P|%c|%m|%G|%t|%X|%Y|%Z"]),
            "partitions": query("scontrol", ["show", "partition", "-o"]),
            "settings": settings,
            "software": software_discovery(config),
            "associations": query("sacctmgr", ["-n", "-P", "show", "association", "where", "user=" + user,
                                                   "format=User,Account,Partition,QOS,DefaultQOS"])}
