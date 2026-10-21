# Protected personal installation

Status: proposed. Date: 2026-10-21.

The agent may read or change its ordinary account's files and operate its desktop. Same-user Docker volumes, secret files and rootless-daemon access do not provide the required separation.

Support a Linux protected profile installed by an administrator under dedicated service ownership. Keep runtime sockets, SSH credentials, deployment code/configuration and administrative credentials outside agent access. Docker Compose packages processes; it does not replace host permissions. The agent receives only project-scoped API credentials. A fake-only same-user development profile remains easy to install.

Strict human approval and MFA use a trusted channel independent of the agent-controlled desktop. This does not isolate arbitrary cluster jobs sharing a remote UID, and it does not protect against root/administrator or kernel compromise. A one-time privileged install is the cost of the stated local protection requirement.
