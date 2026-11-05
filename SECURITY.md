# Security reporting and support

Report vulnerabilities through [GitHub private vulnerability reporting](https://github.com/schmitfe/compute-relay/security/advisories/new).
Do not post credentials or sensitive cluster details publicly. If unavailable,
use an existing trusted private channel to repository owner
[@schmitfe](https://github.com/schmitfe); no public reporting email is designated.
The owner coordinates triage, fixes and disclosure, best effort during beta
without a guaranteed response/resolution time.

Include affected commit/version, settings, violated boundary and a minimal
synthetic reproduction. Do not probe clusters, access others' data, submit jobs
or extract live secrets without operator authorization.

## Supported versions and installations

The latest `0.1` beta and current `main` receive fixes; superseded betas do not.
Preserve outstanding-job evidence and follow [fresh setup](docs/deployment.md#beta-updates-and-fresh-setup)
for incompatible updates. Same-version restart/crash recovery remains supported.

Supported beta scope is one human owner on Linux (CI: Ubuntu 24.04), standard
CPython 3.12–3.14, SQLite, local Waitress and system OpenSSH. It includes
single-node Slurm CPU/GPU jobs, existing environments, owner-authorized
agent-created Python venvs, branching/joining DAGs, advisory storage and
investigations. The [runtime record](docs/runtime.md#acceptance-evidence-and-remaining-reliability-work)
distinguishes real-site and disposable native acceptance. Each institutional
site still needs authentication/accounting/filesystem acceptance; passing a
fixture does not certify arbitrary Slurm configurations.

Apple Silicon macOS 15 is provisional in `0.1.0b1`: the release matrix checks
native filesystem behavior, OpenSSH, installed-wheel restart and MCP, but a
human browser/terminal-MFA/reconnect walkthrough is required before `0.1.0` final.
Intel Macs, Windows, free-threaded Python, shared multi-user hosting, multi-node
jobs and Conda/Poetry environment builders are outside tested support.

The [security model](docs/security.md) defines the supported boundary. Same-account,
remote same-UID, administrator and Docker-daemon access are not isolated. Separate
service/agent UIDs need an isolation walkthrough before recommendation. Automated
[checks](docs/testing.md) are not an external security audit.
