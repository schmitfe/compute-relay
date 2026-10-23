# Portable scientific compute workflow

The broker has one MCP implementation and one optional compute skill. Codex and
Claude Code receive client-specific configuration files, with identical skill
content copied from the installed package. There is no provider API dependency,
provider-specific scheduler implementation or parallel copy of the workflow to
maintain. The skill is planning guidance; policy enforcement stays in the broker.

## Implemented interface

Initialize a science workspace, after creating its project key in the portal:

```sh
compute-broker init-mcp --client both --with-workflow
```

For an existing workspace, explicitly reuse its private key:

```sh
compute-broker init-mcp --client both --reuse-key --with-workflow
```

Pass the same `--broker-url https://broker.example.org` used at initialization
when upgrading a remote setup. The default remains loopback port 8768. Keys are
entered through a hidden terminal prompt, stored in a private ignored file, and
never embedded in client configurations or skill content. No project UUID is
needed. See [setup details](mcp-setup.md) for preservation, conflict and rollback
behavior.

| Client | Generated project configuration | Optional skill |
| --- | --- | --- |
| Codex CLI / desktop sessions using project configuration | `.codex/config.toml` | `.agents/skills/compute/SKILL.md`, small managed `AGENTS.md` block |
| Claude Code | `.mcp.json` | `.claude/skills/compute/SKILL.md`, small managed `CLAUDE.md` block |
| Claude Desktop with local stdio support | Manual application configuration using the same command/env | No automatic skill installation |
| Hosted ChatGPT / Claude connectors requiring HTTP MCP | Not implemented | Requires HTTP transport and authentication work first |

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

```sh
compute-broker files stage --manifest source-bundle.json
compute-broker files prepare --bundle BUNDLE_ID --request job.json --idempotency-key STAGE_KEY
compute-broker files fetch --attempt-id ATTEMPT_UUID --output results/RUN
```

`source-bundle.json` contains explicit relative paths, for example
`{"files":["main.py","src/model.py"]}`. Positional paths also work:
`compute-broker files stage main.py src/model.py`. There is no recursive scan,
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

## Remaining efficiency work

The feedback is separated into these follow-up units so it does not become a
client-specific rewrite or weaken admission controls:

1. **Explainable placement plan.** Add a bounded local capability report produced
   where code actually runs, a workload estimate with uncertainty, and a
   deterministic recommendation. Distinguish a sandbox, laptop and broker host.
   The initial skill already asks for local pilots and justified local/hybrid/
   cluster placement, but no enforced placement planner exists yet.
2. **Workflow identity and optional sub-budgets.** Persist a parent workflow/run
   identifier with each stage. Reserve workflow caps under the same project
   transaction and never issue extra keys to evade the shared allowance. Current
   core idempotency and project reservations already protect concurrent callers.
3. **Bounded waiting and progress.** Add read-only multi-job waiting with bounded
   timeout and status cursors. Keep client disconnection separate from scheduler
   cancellation. Progress should be structured, rate-limited and allowlisted;
   logs must not become an unbounded polling stream.
4. **Server summaries and result lifecycle.** Serve a bounded job summary directly
   from REST, with explicit execution, artifact retention, validation and
   accounting states. Preserve explicit detail access and original evidence.
5. **Optional large-data transport and packed environments.** Keep site-specific
   storage/migration mechanisms outside core submission, configurable for blocked
   ports and filesystem byte/inode quotas. Never infer a quota guarantee from
   advisory free-space information.

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
