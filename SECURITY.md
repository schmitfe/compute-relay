# Security reporting and support

Report vulnerabilities through [GitHub private vulnerability reporting](https://github.com/schmitfe/compute-relay/security/advisories/new).
Do not post credentials or sensitive cluster details publicly. If unavailable,
use an existing trusted private channel to repository owner
[@schmitfe](https://github.com/schmitfe); no public reporting email is designated.
The owner coordinates triage, fixes and disclosure, best effort during alpha
without a guaranteed response/resolution time.

Include affected commit/version, settings, violated boundary and a minimal
synthetic reproduction. Do not probe clusters, access others' data, submit jobs
or extract live secrets without operator authorization.

## Supported versions and installations

Only current `main` receives alpha fixes. Preserve outstanding-job evidence and
follow [fresh setup](docs/deployment.md#alpha-updates-and-fresh-setup) for
incompatible updates; same-version restart/crash recovery remains supported.

Tested scope is standard CPython on Linux, SQLite, local Waitress and system
OpenSSH. The Python matrix is declared in [pyproject.toml](pyproject.toml).
Free-threaded Python, other OSes and arbitrary site-specific Slurm behavior are
unverified. Real sites need recorded authentication/accounting/filesystem
acceptance. A future release must name supported versions/target classes, pass
its gates and retain private reporting; this alpha policy declares no stable release.

The [security model](docs/security.md) defines the supported boundary. Same-account,
remote same-UID, administrator and Docker-daemon access are not isolated. Automated
[checks](docs/testing.md) are not an external security audit.
