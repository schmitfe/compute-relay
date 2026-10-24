"""Reviewed standard-library source component for the fixed one-shot bundle.

This component is assembled with explicit dependencies by bundles.py. It is not
a remotely selected module or an import of submitted/project code.
"""

import json
import os
import sys

from .prelude import MAX_REQUEST


def main(execute):
    """Run the operation dispatcher explicitly supplied by the bundle."""
    try:
        os.umask(0o077)
        payload = sys.stdin.buffer.read(MAX_REQUEST + 1)
        if len(payload) > MAX_REQUEST:
            raise ValueError("Remote request exceeds limit")
        request = json.loads(payload)
        response = execute(request["config"], request)
        sys.stdout.write(json.dumps(response, ensure_ascii=True, separators=(",", ":")))
    except Exception as exc:
        # Nonzero exit is intentionally ambiguous to the caller for submit.
        sys.stderr.write("Broker remote operation failed: " + type(exc).__name__ + "\n")
        sys.exit(1)
