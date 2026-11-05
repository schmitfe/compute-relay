# Fresh-agent acceptance kit

Supervised tasks for a fresh client session, not proof that any client/cluster
passed. Automated tests execute tiny local variants only. Scripts never open SSH,
submit jobs or read tokens; the operator supplies a scoped token path to MCP.

| Case | Placement | Required extension | Evidence |
| --- | --- | --- | --- |
| 00 | One reviewed Slurm job | None | Rejection/revision, approval, native identity, output hash/accounting |
| 01 | Local stdlib diffusion | None | Analytic comparisons; no cluster submissions |
| 02 | Local OU pilot, four cluster replicas, local analysis | None | Distinct seeds, sufficient statistics and uncertainty |
| 03 | Three dependent cluster stages | `workflows` | Native identities and checked dependency hashes |
| 04 | Local theory/pilot, dense quenched cluster simulation | None with approved NumPy | RAM/work/storage/budget plan; transient theory comparison |

`case.json` defines profiles, scopes and bounds. Missing capability/permission
means blocked, never permission to broaden it. Generated projects default to
core-only MCP tools; `--extensions` explicitly narrows registration to available
server modules. The normal launcher bundles storage/environments/workflows, but
builds still need owner policy and `environments:build`. Reviewed wheel closures
can use resumable uploads. Manual artifact relay does not pass case 03's workflow
acceptance; arrays/native scheduler dependencies are unsupported.

## Create a run

Use a fresh operator-configured project and destination per client/case repetition:

```sh
python tools/agent_integration.py create --case 00 \
  --destination /tmp/broker-case00-codex \
  --broker-url http://127.0.0.1:8768 \
  --project-token-file /ABSOLUTE/PROJECT_TOKEN_FILE \
  --python /ABSOLUTE/BROKER_VENV/bin/python
```

Python must already contain Compute Relay, including its core MCP dependency.
The offline generator references but never opens/copies the token; authenticated
MCP resolves project identity. It creates private `0700` directories/`0600` files,
refuses overwrite, and permits only `/tmp/` or ignored checkout `local/` destinations.
It writes project client settings, scripts/prompts, source hashes and a recording
template; global client settings remain untouched. Add
`--extensions environments,workflows` only for enabled and explicitly granted
capabilities. Exhausted slots, small grants or uncertain timing never authorize
larger limits; high-RAM work needs its own accepted grant/plan.

Open/trust the generated project, verify `compute_relay` is connected, then send
`PROMPT.md`. Codex uses `.codex/config.toml`; Claude Code uses `.mcp.json`.
Direct browser/ChatGPT HTTP connectors are unsupported. Client setup references:
[Codex](https://learn.chatgpt.com/docs/extend/mcp?surface=cli),
[Claude Code](https://code.claude.com/docs/en/mcp).

## Record evidence

Complete `record.template.json` as `run-report.json`, retaining every rubric ID.
Record public identities, native accounting, hashes, versions/model, parameters
and limitations, never keys or private service settings:

```sh
python tools/agent_integration.py record --project /tmp/broker-case00-codex \
  --input run-report.json --name codex-run-01
```

The non-overwriting recorder validates format, not truth. Compare transcript,
reviewed package, native labels and artifacts independently. Fake execution cannot
pass Slurm cases. Reconcile failures/cancellations/uncertainty without blindly
resubmitting. Reserve effective CPUs × requested whole-minute walltime; settle
actual allocated usage, including overages. All cases use zero GPUs. Include
pilots/builds/failures/replicas in the allowance and data/logs in storage estimates.

## Local verification

```sh
python -m pytest tests/unit/test_agent_integration_kit.py -q
```

Optional NumPy tiny checks require matching CPython 3.12/3.13 Linux x86_64 wheels:

```sh
python -m pip install --require-hashes \
  -r examples/agent-integration/04-dense-ising/requirements-science.lock \
  --target local/science-packages
PYTHONPATH=local/science-packages python -m pytest tests/unit/test_agent_integration_kit.py -q
```

Ordinary tests install no science dependencies or large matrices. High-N tests
check arithmetic only. Large execution requires explicit parameters, matching
accepted plan, compute-node memory verification and `--execute-large`; broker
policy/human decisions remain authoritative. Other platforms need reviewed wheel
locks. [References](REFERENCES.md) states the model, RNG and dependency limits.
