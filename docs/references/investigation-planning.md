# Investigation planning and stopping rules

Reviewed 2026-10-27. This bounded comparison uses two installed skills from
`mattpocock-skills` 1.3.1 and four first-party research/orchestration sources.
The upstream skill links were checked against the installed instructions; web
documentation is a snapshot accessed on the review date. No upstream code,
production service or scientific compute was run.

The motivating investigation used individual job tools. Its feedback is evidence
about that experience, not a test of Compute Relay's native
`prepare_workflow` / `start_workflow` / `advance_workflow` engine.

## Recommendation

Add a short investigation plan before the placement/execution loop, then use it
at each decision to expand work. Define the question, minimum useful deliverable,
core experiments, necessary validation, a finite discrepancy allowance, resource
envelope and stopping conditions. Reserve effort for analysis and reporting.
Unused compute allowance is capacity, not a scientific reason to run more jobs.

These are recommendations for agent behavior. A local plan does not create an
enforced investigation budget, reserve project funds, or certify scientific
validity. The broker's admission and accounting controls remain independent.

## What the comparison supports

| Source | Existing operational rule | Useful adaptation and limitation |
| --- | --- | --- |
| [Diagnosing Bugs skill](https://github.com/mattpocock/skills/blob/main/skills/engineering/diagnosing-bugs/SKILL.md), installed 1.3.1 | Establish a reproducible signal, minimize the case, rank falsifiable hypotheses, and connect each probe to a prediction. Stop and disclose when a usable feedback loop cannot be built. Recheck the original symptom and record the explanation before finishing. | Make discrepancy work a small, discriminating experiment rather than an undirected sweep. The skill also encourages persistence; it supplies no cumulative time/compute ceiling. Copy the evidence discipline together with an explicit allowance. |
| [Prototype skill](https://github.com/mattpocock/skills/blob/main/skills/engineering/prototype/SKILL.md), installed 1.3.1 | Start with the question the prototype must answer, minimize implementation/polish, and capture the verdict plus its supporting artifact. | Define the output of a pilot before running it: a scaling estimate, feasibility decision or specific diagnostic result. A runnable prototype alone does not answer the question. Its throwaway-code rules do not replace scientific provenance/retention requirements. |
| [Anthropic multi-agent research system](https://www.anthropic.com/research/multi-agent-research-system) | Reports agents continuing after sufficient results and duplicating work. Delegation needs an objective, output format, source/tool guidance and clear boundaries; effort should scale with query complexity. Persist plans and artifact references across handoffs. | Give each delegate a bounded question, required artifact and stop condition. One coordinator should decide whether another round is justified. The article's tool-call examples are search heuristics, not portable limits on GPU experiments or demonstrated cost guarantees. |
| [Claude Managed Agents session budgets](https://platform.claude.com/docs/en/managed-agents/budgets) | Shares one session budget across threads and stops new model requests at the cap; requests already in flight finish. A paused session preserves its history. | Shared accounting must include concurrent agents, and stopping admission is different from cancelling work or losing evidence. This is an example of runtime enforcement for that product; its token/dollar accounting is not Compute Relay's resource ledger and does not establish an exact physical spending bound. |
| [OpenAI Agents SDK running agents](https://openai.github.io/openai-agents-python/running_agents/) | Distinguishes final output, handoffs and tool-loop continuation; exceeding `max_turns` raises a specific exception. A paused run can be resumed from saved state. | Define a separate bounded orchestration loop and retain a recoverable checkpoint on exhaustion. A model-turn limit alone does not express scientific sufficiency or bound resource use within a tool call. This is a runtime feature of that SDK, not a guarantee supplied by a compute skill. |
| [Center for Open Science preregistration guidance](https://www.cos.io/initiatives/prereg) | Distinguishes planned and unplanned analyses, discloses plan changes and supports separating exploration from confirmation, including held-out data. | Record core questions/checks before inspecting results, retain null or inconclusive outcomes, and label later extensions. A practical investigation plan need not be a formal registered study; planning alone does not validate adaptive statistical stopping or make an exploratory result confirmatory. |

## Proposed rules for the compute skill

The following synthesis is a design recommendation, rather than a claim that all
six sources prescribe the same workflow.

1. **Write a proportionate plan once.** Use the user's requested outcome and
   existing authorization. Record the question, relevant assumptions and a
   concrete deliverable: for example, a comparison table with uncertainty, a
   reproducible figure or a feasibility verdict. State exclusions. For a small
   task, a few lines in the run record suffice; a separate lengthy planning
   conversation is unnecessary. Ask only for material choices that cannot be
   inferred from the request and available project context.
2. **Separate the core from extensions.** Enumerate the smallest experiment set
   that answers the question, plus required validity checks such as units,
   residuals, expected limits, precision and convergence. Classify speculative
   parameter sweeps and additional mechanisms as optional extensions. Failed
   required validation can make the answer inconclusive; it does not authorize
   unlimited debugging or permit presenting the original result as valid.
3. **Bound both resources and exploration.** Record finite limits in explicit
   units: allocated CPU core-seconds, GPU device-seconds, per-job walltime,
   number of attempts/diagnostic rounds and relevant storage. Include local
   compute and orchestration time where material. Use an existing requested
   budget when present; otherwise state a conservative finite plan within the
   user's authorization. Project balance is shared capacity, not an instruction
   to consume it. Include pilots, failed runs, retries and environment probes
   that require compute. Leave enough room to analyze, verify, collect and report.
4. **Require a decision for every extra experiment.** Record the unresolved
   discrepancy, competing explanations, proposed distinguishing observation,
   expected effect on the conclusion and estimated resource use. Consume a
   finite diagnostic allowance already in the plan. A numeric cap is useful
   only in context; there is no evidence here for a universal percentage or
   fixed number of supplementary experiments. Further expansion requires an
   explicit scope/budget update within the user's authority.
5. **Stop when any stopping condition applies.** Finish when the core deliverable
   and required checks are complete. Also stop starting new work when the
   diagnostic allowance, resource envelope or deadline is reached, a named
   blocker prevents valid progress, or another experiment has no clear bearing
   on the requested decision. Record whether the outcome is supported,
   inconclusive or blocked. Do not automatically enlarge the experiment set
   because results are interesting, a desired effect is absent, or funds remain.
6. **Share the plan across delegation and recovery.** One coordinator owns the
   investigation identifier, plan revision and next spending decision. Assign
   distinct tasks, deliverables, finite limits and stop conditions to delegates;
   all use the same project connection. Count their combined work and
   outstanding reservations. A handoff, new agent, retry or context reset does
   not reset the plan's allowance. Local sub-allocations are coordination rules;
   they do not add broker-enforced sub-budgets or atomically reserve headroom.
7. **Make closeout a planned stage.** Stop proposing extensions, inventory related
   jobs/drafts, cancel abandoned unsubmitted work, collect required artifacts,
   verify downloads and write the scientific report. Record executions,
   validation, retained outputs, local verification, accounting and
   acknowledgement separately. Preserve pending accounting, uncertain
   submissions, missing outputs and unresolved scientific discrepancies as
   explicit outstanding items. Report useful findings even when uncertainty
   prevents the preferred conclusion. Cleanup/reconciliation may continue
   without starting another scientific experiment.

These rules combine question-first artifacts and discriminating probes from the
two installed skills with bounded delegation, preserved state and transparent
planned-versus-exploratory reporting from the first-party sources above. They
should be evaluated with representative agent tasks: a clean result, failed
convergence, an interesting optional lead, exhausted diagnostics, concurrent
delegates, context recovery and unsettled accounting. Check whether the agent
produces the promised artifact and stops at the recorded boundary, rather than
requiring a single exact sequence of tool calls.

## Enforcement and scientific tradeoffs

Compute Relay's [accounting contract](../../contracts/README.md) reserves against
the prepared allocation and requested walltime, then charges actual allocated
runtime. A reservation is not a physical upper bound; missing final accounting
keeps uncertainty visible. The plan must count outstanding commitments and
preserve margin instead of equating settled charges with total exposure.
Recording a smaller investigation limit in Markdown does not install a new
transactional broker limit. Financial prices, model tokens, allocated resource
time, storage and scientific scope are different quantities.

Stopping early can preserve resources while leaving a question unresolved. The
correct response is a bounded, honest report with the missing check and its
implication, followed by a concrete proposal if further work is warranted.
Conversely, treating every diagnostic as optional would weaken the science:
necessary validation remains necessary for the strength of the claim, even when
there is no remaining authorization to perform it. Statistical precision or
convergence thresholds should have a justified analysis rule; repeatedly testing
until a favorable result appears is not a valid substitute.

Skill changes should therefore make finishing and disclosure routine while
preserving flexibility to revise an explicit plan when the user wants a broader
investigation. They should not introduce per-job confirmation when existing
authorization already covers the agreed work.
