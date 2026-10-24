# Local account boundary

Status: supersedes the earlier protected-service proposal.

The supported package runs under one local operating-system account. It does
not install a dedicated service identity or claim secret isolation from that
same account. Optional portal passwords, project scopes and approval pages do
not change this boundary. See [security](../security.md).

A separate-account, VM or hosted installation would require its own design and
acceptance. The earlier proposal is retained only in the archive identified by
the repository README.
