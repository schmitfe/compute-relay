# What has and has not been run

Local verification on21October2026 used CPython3.13.9. The kit's focused suite passed35 tests with optional NumPy2.3.4 available. The repository suite passed422 tests and64 subtests; its default environment skipped the optional NumPy test. A separate local CLI sweep executed11 tiny commands: smoke, diffusion, two OU samples and aggregation, three regression pipeline stages, tiny Ising, bounded512² calibration and memory inspection. Combined output was under16KiB. No large matrix was allocated.

No fresh Codex/Claude session, real MCP client connection, institutional SSH connection, environment build, native workflow, or real Slurm job was run for this kit. Those acceptance records remain `not_run`. The kit is a reproducible starting point for supervised acceptance, not a client compatibility certificate or a benchmark result.

Run the standard tests without optional science dependencies:

```sh
python -m pytest tests/unit/test_agent_integration_kit.py -q
```

To exercise the NumPy tiny path in a disposable local dependency directory, on a matching CPython3.12/3.13 Linux x86_64 interpreter:

```sh
python -m pip install --require-hashes \
  -r examples/agent-integration/04-dense-ising/requirements-science.lock \
  --target local/science-packages
PYTHONPATH=local/science-packages python -m pytest tests/unit/test_agent_integration_kit.py -q
```

The actual cp312 wheel was downloaded and hashed, but not executed here; the cp313 wheel was executed. Dependencies are not installed by ordinary tests. Other platforms must generate/review their own compatible wheel lock. No wheel binaries are committed.

High-N tests perform integer/resource arithmetic only. An ordinary script invocation prints help; a large simulation requires explicit parameters, a matching accepted resource plan, current compute-node memory verification, and `--execute-large`. The planner is an acceptance guard, not an authorization boundary: broker policy, human decisions and Slurm remain authoritative.
