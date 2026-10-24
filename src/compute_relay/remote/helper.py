"""Local inspection facade for fixed, reviewed remote bundle components.

Production transports use bundles.bundle_source with an explicit operation
family. This facade is not transmitted or hashed as an execution helper. Only
installed, fixed-allowlist first-party definitions are compiled here; prepared
packages and journals never supply Python source.
"""

from .bundles import local_inspection_source

exec(compile(local_inspection_source(), "<compute-relay-local-helper>", "exec"))
