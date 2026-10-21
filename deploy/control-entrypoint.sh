#!/bin/sh
set -eu
umask 077

# Synthetic bootstrap is opt-in. Real installs enroll through a trusted channel.
if [ "${BROKER_BOOTSTRAP_DEMO:-0}" = "1" ]; then
    python -m compute_broker.cli bootstrap-demo \
        --output /run/demo --backend "${BROKER_DEMO_BACKEND:-slurm}"
    # Keep owner credentials outside the worker's mounted configuration volume.
    install -m 0600 /run/demo/worker.json /run/worker/worker.json
    install -m 0600 /run/demo/worker-token /run/worker/worker-token
fi

exec python -m compute_broker.control "$@"
