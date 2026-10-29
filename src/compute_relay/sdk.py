"""Project-local Python access to the same operations used by the MCP adapter."""

from .mcp.facade import ProjectFacade
from .worker.client import ControlError
from .workspace import WorkspaceClient
from .workspace_completion import finish

__all__ = ["ControlError", "ProjectFacade", "WorkspaceClient", "finish"]
