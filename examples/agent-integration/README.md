# Fresh-agent acceptance kit

These are reproducible tasks for a new agent session, not evidence that a client or cluster has already passed. The repository's automated tests run only tiny local variants. No script in this kit submits a job, opens SSH, starts a tunnel, or reads a project token. An operator supplies a project-scoped token file to the stdio facade.

| Case | Intended placement | Default required extensions | Success evidence |
| --- | --- | --- | --- |
| 00 | One reviewed, tiny native Slurm job | None | Rejection/comment, revised source, exact approval, native identity, output hash, settled accounting |
| 01 | Local stdlib diffusion | None | Analytic discrete/continuum comparison; zero cluster submissions |
| 02 | Local OU derivation/pilot, four cluster replicas, local analysis | None | Distinct seeds, exact transition, retrieved sufficient statistics and uncertainty |
| 03 | Three artifact-dependent cluster stages | `workflows` | Native task identities and checked data → model → validation dependency hashes |
| 04 | Local theory/pilot; genuinely dense quenched disorder on suitable cluster | None with an existing approved NumPy environment | Effective-RAM, work/storage/budget preflight; transient simulation/theory comparison |

Extensions are disabled by default. `case.json` declares required extensions, core tools, optional profiles and resource bounds. Discover actual tools/capabilities first. Missing extension or scope means **blocked**, not permission to enable server extensions or broaden a key. Environment building needs the human-enabled `environments` extension. The current alpha can build an empty stdlib venv or a small verified wheel closure; its512KiB core upload limit cannot stage the16.6MB NumPy wheel, so a managed NumPy build is blocked until large-wheel transport exists. Use a preinstalled approved NumPy environment for case04; arrays and native dependency orchestration need the advertised workflow capabilities. Ordinary output retrieval is core. The manual artifact-relay fallback for case03 does not pass native-workflow acceptance.

From the repository, create one fresh private project:

```sh
python tools/agent_integration.py create --case 00 \
  --destination /tmp/broker-case00-codex \
  --broker-url http://127.0.0.1:8768 \
  --project-id YOUR_PROJECT_UUID \
  --project-token-file /ABSOLUTE/PROJECT_TOKEN_FILE \
  --python /ABSOLUTE/BROKER_VENV/bin/python
```

Use a new operator-configured project for each externally metered run. A consumed lifetime slot, a256MiB supervised profile, or an unverified timing profile is not permission to enlarge limits. High-RAM verification requires a separately configured suitable grant; otherwise record blocked.

The supplied Python must already have this broker and its `mcp` extra installed. The generator creates mode0700 directories and mode0600 files, refuses any existing destination, and references the token path without opening/copying it. Destinations must be below `/tmp/` or this checkout's ignored `local/`. It changes no global client settings. Optional client registration is explicit: add --extensions environments,workflows only when the human has enabled those server extensions and granted the required project/key scopes; the flag does not grant authority. Each project contains `.codex/config.toml`, `.mcp.json`, `PROMPT.md`, scripts, checklist, source hashes, and a recording template. Use a distinct project directory/session for every client/case repetition.

For Codex, open the generated project, review/trust its project configuration, verify the `compute_broker` server is connected, and send `PROMPT.md` as the first task. Codex documents project-scoped stdio configuration in `.codex/config.toml`. [Official Codex MCP documentation](https://learn.chatgpt.com/docs/extend/mcp?surface=cli).

For Claude Code, start in that project and review its project `.mcp.json`; check the MCP connection before sending the prompt. [Official Claude Code MCP documentation](https://code.claude.com/docs/en/mcp).

The ordinary ChatGPT connector/UI is **not directly supported by this stdio-only alpha**. A Codex surface in a desktop app is a separate client test. ChatGPT's connector setup expects an accessible HTTP MCP endpoint; none is implemented or exposed by this kit. [Official ChatGPT MCP quickstart](https://developers.openai.com/plugins/build/app-quickstart). No tunnel is created automatically.

Complete `record.template.json` into `run-report.json`, retaining every check ID. Record public job/attempt IDs, native backend identity, hashes, measured allocated compute, storage, versions/model and limitations; never paste keys or private service configuration. Save a validated, nonoverwriting result:

```sh
python tools/agent_integration.py record --project /tmp/broker-case00-codex \
  --input run-report.json --name codex-run-01
```

The recorder checks format/rubric completeness; it does not independently attest that an agent's claims are true. A human must compare the transcript, broker review, native backend labels and artifacts. Fake execution must remain labeled simulated and cannot pass real-Slurm cases. Failed/cancelled/uncertain jobs still need accounting reconciliation; never erase them or repeat an ambiguous submission. Use stable idempotency keys for retries of the same intended mutation.

Requested scheduler time is whole minutes. Read maximum charged duration from the prepared manifest, including the site's verified margins; budget CPU cores × maximum duration, not measured utilization. GPU count is zero in every case. Include builds, pilots, failed attempts, copies, logs and all replicas in resource planning. The broker remains the admission authority.

NumPy is optional and only used in case04's bounded calibration/large implementation. Its wheel-specific lock contains an actual downloaded wheel hash for CPython3.12/3.13 Linux x86_64, not a fabricated universal hash. Another platform needs a reviewed wheel/hash lock. No raw dense coupling matrix is written to shared storage. See `REFERENCES.md` and case04's prompt for the model's limits and honest memory-efficient alternatives.
