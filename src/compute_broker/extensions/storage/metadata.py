"""Navigation and permissions advertised only while this app is enabled."""

KEY_SCOPES = []  # Read-only planning uses the existing project jobs:read scope.
PROJECT_LINKS = [("Storage", "/projects/{project_id}/storage/")]
NAV_LINKS = [("Storage targets", "/storage/targets/")]
