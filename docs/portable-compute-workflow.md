# Portable scientific compute workflow

The broker has one MCP implementation and one optional compute skill. Codex and
Claude Code receive client-specific configuration files, with identical skill
content copied from the installed package. There is no provider API dependency,
provider-specific scheduler implementation or parallel copy of the workflow to
maintain. The skill is planning guidance; policy enforcement stays in the broker.

## Implemented interface

Initialize a science workspace, after creating its project key in the portal:

```sh
compute-relay init-mcp --client both --with-workflow
```

For an existing workspace, explicitly reuse its private key:

```sh
compute-relay init-mcp --client both --reuse-key --with-workflow
```

Pass the same `--broker-url https://broker.example.org` used at initialization
when upgrading a remote setup. The default remains loopback port 8768. Keys are
entered through a hidden terminal prompt, stored in a private ignored file, and
never embedded in client configurations or skill content. No project UUID is
needed. See [setup details](local-client-setup.md) for preservation, conflict and rollback
behavior.

| Client | Generated project configuration | Optional skill |
| --- | --- | --- |
| Codex CLI / desktop sessions using project configuration | `.codex/config.toml` | `.agents/skills/compute/SKILL.md`, small managed `AGENTS.md` block |
| Claude Code | `.mcp.json` | `.claude/skills/compute/SKILL.md`, small managed `CLAUDE.md` block |
| Claude Desktop with local stdio support | Manual application configuration using the same command/env | No automatic skill installation |
| Browser-based MCP integrations | Not implemented | This package configures project-local stdio clients only |

Client trust prompts and tool availability remain client-controlled. Delegates
do not necessarily inherit their parent's MCP tools. The skill tells the parent
to mediate operations when necessary. Every participant using the same project
shares its existing atomic compute ledger and submission limits. This change
does not create separate delegate budgets or introduce workflow budget caps.

The configuration formats and discovery paths follow the official
[Codex MCP documentation](https://learn.chatgpt.com/docs/extend/mcp?surface=cli),
[Codex skills documentation](https://learn.chatgpt.com/docs/build-skills),
[Claude Code MCP documentation](https://code.claude.com/docs/en/mcp) and
[Claude Code skills documentation](https://code.claude.com/docs/en/skills).
Automated acceptance verifies generated files and real MCP stdio calls, not the
interactive behavior of every released desktop client.

## Bounded conversation traffic

One fresh `get_project_context` call supplies project identity, current targets,
cluster-bound environments, effective limits, review reasons and allowance
when the key has `jobs:read`. Separate discovery tools remain available for
targeted refresh. The broker checks authority and limits again on mutations;
a context snapshot is not an admission permit.

`prepare_job`, `revise_job` and `get_job` return compact job summaries by default.
They include current validation, reservation, approval, matching execution and
attempt, safe scheduler/accounting facts, result availability and next action.
Old attempts are not presented as results of a newer rejected or pending
execution. Full metadata requires an explicit `get_job(detail=True)` call.
Accounting distinguishes requested resources, observed allocation, actual
charged CPU/GPU seconds and the difference from the reservation. Missing final
accounting remains unknown. Output existence and scientific correctness are
not inferred from scheduler success.

Responses identify their mode explicitly: jobs use `response_mode: compact` or
`detail`; `get_project_allowance()` uses `budget`, omitting the repeated target
catalog, while `detail=True` retains it. `get_project_context.adapter` and the
MCP initialization instructions identify the local adapter's package version,
schema version and source fingerprint captured when that process starts. This
helps identify an old running adapter after an upgrade. It is a diagnostic,
not a signature or a version assertion about the separately deployed broker.

The tool request schema derives from the broker's shipped JSON schema,
including nested resource, environment, file and output fields. Invalid inputs
fail before HTTP submission without echoing their values. Logs default to
8 KiB chunks, with explicit reads up to 64 KiB. The existing artifact-content
tool remains available for deliberately small reads.

Compaction currently happens in the local MCP adapter after fetching the full
REST job response. It reduces the response entering the conversation, not yet
the underlying HTTP transfer or server history query. A synthetic long-history
test measures serialized response bytes, not vendor token usage or a universal
performance guarantee.

## Workspace source and result helpers

Run these ordinary CLI operations where the initialized project files live:

Initialization writes `.compute-relay/USAGE.md` with the actual installed
interpreter and workspace path, correctly quoted for the shell. Prefer those
exact `python -m compute_relay.cli ...` commands when the console script is
absent from the client's search path. Refresh a known generated workflow with
`init-mcp --reuse-key --with-workflow`; locally modified guidance is preserved
by refusing an unrecognized replacement.

```sh
compute-relay files stage --manifest source-bundle.json
compute-relay files prepare --bundle BUNDLE_ID --request job.json --idempotency-key STAGE_KEY
compute-relay files fetch --attempt-id ATTEMPT_UUID --output results/RUN
```

`source-bundle.json` contains explicit relative paths, for example
`{"files":["main.py","src/model.py"]}`. Positional paths also work:
`compute-relay files stage main.py src/model.py`. There is no recursive scan,
implicit upload of the whole workspace, archive extraction or broker-host file
access. Each command accepts `--directory /absolute/project`.

Staging checks files before contacting the service, computes hashes in ordinary
code, checks project-owned content through the bounded `objects/lookup` API and
uploads only missing bytes. A private bundle manifest records hashes, sizes,
logical paths and executable intent. The printed receipt contains the bundle ID
and counts. Repeating staging reuses server content. Preparation merges this
manifest into a job JSON request without `files` or `project_id`, verifies it
against the normal schema and prepares the package. It does not request
execution. By default preparation derives a stable idempotency key from the
complete request; supply a different explicit key for a deliberate new package.

Fetching uses authorized artifact IDs, validates the complete inventory before
creating the destination, verifies each byte count and SHA-256, and writes a
receipt only after successful collection. It never overwrites an existing
output directory. The receipt explicitly records `scientific_validation:
"not_checked"`; the agent must still analyze the result. A failed download can
leave verified partial output without a success receipt. Retry into a fresh
directory after resolving the cause.

| Helper boundary | Current maximum |
| --- | --- |
| Explicit source files | 100, 1 MiB each, 16 MiB total |
| Retained output files | 100, 10 MiB each, 50 MiB total |
| Lookup body | 32 KiB; only project-owned hashes and sizes |

Symlinks, source hard links, special files, traversal, absolute paths, private
broker/client/Git/SSH directories and reserved broker paths are rejected.
Destinations use no-follow directory descriptors and exclusive file creation.
The saved workspace connection and key must be owned by the current user and
private. These boundaries protect against accidental selection and path races;
they do not isolate secrets from a hostile process running as the same OS user.
Cluster identities still live with the human-owned workers. The helper has only
the existing project key and cannot approve jobs or alter cluster configuration.

This is the bounded broker-retained artifact path. Direct cluster-to-workspace
transfers, remote filesystem migration and large scientific datasets remain
separate optional extension work.

## Investigation planning and completion

The skill now defines a deliverable, core experiments, required validation,
resource/execution limits, diagnostic allowance and stopping rule before
substantive compute. All delegates share the same plan and counters. Read the
[planning comparison](references/investigation-planning.md) for the research and
skill patterns behind these rules. Scope completion and budget exhaustion are
separate decisions; reporting inconclusive is an explicit valid outcome.

Shared guidance is discoverable through MCP resources and get_compute_guide.
Core get_job_completion and the local files finish command consolidate evidence
and verified delivery; get_job supports bounded revision-based waiting. The
explicitly enabled investigations module adds metadata grouping, roles, filtered
recovery, aggregate ledger accounting and scope closeout. See
[investigations and completion](investigations.md) for enablement and limitations.

## Remaining efficiency work

There is no client-specific implementation backlog. Conditional improvements
and their priority are tracked in the single [roadmap](roadmap.md); current
placement advice, investigation limits and response bounds remain advisory and
bounded as documented here.

Acceptance should run the same scientific prompt and assertions through each
client: local derivation/small test, hybrid pilot/sweep, cluster multi-stage
simulation, review rejection/revision and shared-budget contention. The
[fresh-agent science kit](../examples/agent-integration/README.md) is the starting
point. Real cluster submissions and desktop trust/MFA flows remain explicit
operator acceptance, not ordinary unit tests.

Automated regression coverage is in `test_portable_mcp_setup.py` (both client
formats, preservation, private paths and rollback), `test_mcp_responses.py`
(canonical tool schema, safe validation, bounded summaries and accounting),
`test_workspace.py` (path boundaries, content reuse and real HTTP CLI staging/
preparation/download) and `test_portal_theme.py` (actual theme JavaScript,
persistence and palette contrast). Browser acceptance uses a separate synthetic
portal and checks the dashboard, project forms and review page. It does not
submit a job to an institutional cluster or change live project policies.

## Portal theme

The header offers **System**, **Light** and **Dark**. System is the default;
explicit preferences are saved only in that browser and applied before styling.
The palette covers cards, forms, validation, tables and logs. No database field,
external asset or network service is required. With storage disabled the selector
still works for the current page; without JavaScript the system palette applies.
