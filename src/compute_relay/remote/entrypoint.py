"""Reviewed standard-library source component for the fixed one-shot bundle.

This component is assembled with explicit dependencies by bundles.py. It is not
a remotely selected module or an import of submitted/project code.
"""

import sys

def main():
    os.umask(0o077)
    payload = sys.stdin.buffer.read(MAX_REQUEST + 1)
    if len(payload) > MAX_REQUEST:
        raise ValueError("Remote request exceeds limit")
    request = json.loads(payload)
    response = execute(request["config"], request)
    sys.stdout.write(json.dumps(response, ensure_ascii=True, separators=(",", ":")))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Nonzero exit is intentionally ambiguous to the caller for submit.
        sys.stderr.write("Broker remote operation failed: " + type(exc).__name__ + "\n")
        sys.exit(1)
