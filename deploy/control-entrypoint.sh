#!/bin/sh
set -eu
umask 077

# Synthetic bootstrap is opt-in. Real installs enroll through a trusted channel.
if [ "${BROKER_BOOTSTRAP_DEMO:-0}" = "1" ]; then
    python -m compute_relay.cli bootstrap-demo \
        --output /run/demo --backend "${BROKER_DEMO_BACKEND:-slurm}"
    # Keep owner credentials outside the worker's mounted configuration volume.
    install -m 0600 /run/demo/worker.json /run/worker/worker.json
    install -m 0600 /run/demo/worker-token /run/worker/worker-token
fi

exec python - <<'PY'
import faulthandler
import os
import signal

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "compute_relay.control.settings")
faulthandler.register(signal.SIGUSR1, all_threads=True)
from django.core.wsgi import get_wsgi_application
from waitress import serve
serve(get_wsgi_application(), host=os.environ.get("BROKER_HOST", "0.0.0.0"),
      port=int(os.environ.get("BROKER_PORT", "8765")), threads=4,
      expose_tracebacks=False, max_request_body_size=16 * 1024 * 1024,
      ident="Compute Relay")
PY
