"""Test-only installation enables bundled apps present in this checkout.

The local launcher explicitly selects its saved modules; these lower-level tests
enable installed modules by default.
Explicit BROKER_EXTENSIONS always wins, including an explicitly empty list.
"""

import os
from pathlib import Path

if "BROKER_EXTENSIONS" not in os.environ:
    root = Path(__file__).resolve().parents[1] / "extensions"
    os.environ["BROKER_EXTENSIONS"] = ",".join(
        name for name in ("storage", "environments", "workflows", "investigations")
        if (root / name / "apps.py").is_file())

from .settings import *  # noqa: F401,F403,E402
