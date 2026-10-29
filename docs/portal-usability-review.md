# Portal usability review — 2026-10-29

Reviewed merged main `5ae8d58` using four independent agent roles: a researcher
new to Slurm, an experienced Slurm user, a UX engineer and a validator. The first
three inspected forms, templates, routes and documentation; the validator
checked their claims against services and isolated Django HTTP tests. The lead
also inspected the rendered dashboard, project creation/detail, key creation,
cluster creation, environment list/creation and storage pages in a disposable
loopback installation with synthetic data and all four optional modules enabled.

These are simulated user perspectives and verified application behaviors, not
interviews or evidence that real users completed the journey. No real cluster,
saved installation or credential was used. The findings describe the reviewed
base commit; their source links are pinned to that version. The implementation
below addresses them. The [roadmap](roadmap.md) remains the single backlog.

## Validated findings and smallest useful changes

1. **Older running work disappears from ordinary job navigation.** Dashboard,
   project and live-update queries all select the latest 100 jobs. An isolated
   probe created one older running job and 100 newer drafts: the running job was
   absent from all four lists, although its direct review URL still worked.
   Pending approvals also have a separate notification list showing the oldest
   20 requests and a total count; this does not solve running-job navigation.
   Add bounded history navigation and state filters, including unresolved
   accounting/output collection; preserve the selected view during live updates.
   Raising the list limit merely moves the failure. Evidence:
   [dashboard](https://github.com/schmitfe/compute-relay/blob/5ae8d589338ae93107fd72af0be7a07178cdc824/src/compute_relay/control/views.py#L437),
   [project](https://github.com/schmitfe/compute-relay/blob/5ae8d589338ae93107fd72af0be7a07178cdc824/src/compute_relay/control/management.py#L199),
   [live lists](https://github.com/schmitfe/compute-relay/blob/5ae8d589338ae93107fd72af0be7a07178cdc824/src/compute_relay/control/live_updates.py#L36).

2. **A failed review submission loses the comment.** A wrong-manifest rejection
   correctly left the request awaiting approval, but returned an empty comment
   field. Preserve the escaped submitted draft only for its matching execution
   request. Keep the current manifest and authorization checks. Live polling
   already preserves drafts and must continue doing so. Evidence:
   [review handler](https://github.com/schmitfe/compute-relay/blob/5ae8d589338ae93107fd72af0be7a07178cdc824/src/compute_relay/control/views.py#L448),
   [comment field](https://github.com/schmitfe/compute-relay/blob/5ae8d589338ae93107fd72af0be7a07178cdc824/src/compute_relay/control/templates/broker/live_execution_request.html#L6).

3. **Two choices make no useful decision.** The key's **Follow project policy**
   and **Automatic within project policy** produce the same review requirements;
   the validator checked all 18 combinations of project mode, cluster opt-in and
   envelope status. Remove the latter choice consistently from forms, stored
   choices, callers and tests. Keep **Always require human confirmation**. The
   cluster form's backend dropdown has only **Slurm**; remove that visible choice
   while retaining the internal backend discriminator. Evidence:
   [key form](https://github.com/schmitfe/compute-relay/blob/5ae8d589338ae93107fd72af0be7a07178cdc824/src/compute_relay/control/forms.py#L105),
   [review gates](https://github.com/schmitfe/compute-relay/blob/5ae8d589338ae93107fd72af0be7a07178cdc824/src/compute_relay/control/execution_policy.py#L24),
   [cluster form](https://github.com/schmitfe/compute-relay/blob/5ae8d589338ae93107fd72af0be7a07178cdc824/src/compute_relay/control/cluster_setup.py#L29).

4. **Cluster and environment grants require an unexpected extra save.** A new
   project lists recipes even with no cluster checked. An existing project only
   renders recipes for its saved clusters, so selecting another cluster requires
   a server round trip before its recipes can be selected. Present owned recipes
   grouped by cluster and update the visible groups together; keep server-side
   validation and explicit grants. No automatic permission for future recipes.
   Evidence: [project form](https://github.com/schmitfe/compute-relay/blob/5ae8d589338ae93107fd72af0be7a07178cdc824/src/compute_relay/control/forms.py#L56),
   [initial values](https://github.com/schmitfe/compute-relay/blob/5ae8d589338ae93107fd72af0be7a07178cdc824/src/compute_relay/control/management.py#L153).

5. **Setup has several avoidable decisions and weak handoffs.** Cluster creation
   shows nine logical fields, including the single-choice backend and SSH
   overrides. Keep name, hostname and username prominent; collapse port, local
   key/known-hosts paths, directory hint and session lifetime. Each cluster state
   should identify its next required action using existing readiness state.
   Project creation has 15 logical fields in one flat list: group policy and
   total allowance, maximum resources per job, concurrency/lifetime limits, and
   access. Add direct links to settings on the project page. The one-time key
   screen should finish with the existing `compute-relay init-mcp` instructions:
   run in the science directory, paste the key into the hidden terminal prompt,
   then start a fresh client session. Never put the key in chat, a URL or command
   arguments. Evidence: [cluster page](https://github.com/schmitfe/compute-relay/blob/5ae8d589338ae93107fd72af0be7a07178cdc824/src/compute_relay/control/templates/broker/cluster_form.html),
   [project page](https://github.com/schmitfe/compute-relay/blob/5ae8d589338ae93107fd72af0be7a07178cdc824/src/compute_relay/control/templates/broker/project_detail.html),
   [key handoff](https://github.com/schmitfe/compute-relay/blob/5ae8d589338ae93107fd72af0be7a07178cdc824/src/compute_relay/control/templates/broker/key_created.html),
   [client setup](local-client-setup.md#initialize-a-science-project).

6. **Labels and units force users to translate policy.** The form grants two
   CPU core-hours while the allowance table displays 7,200 seconds. Resource
   ceilings are labelled "CPUs per job" rather than "Maximum CPUs per job";
   "Mode" is less useful than "Execution policy". Cluster walltime is entered in
   seconds while its help asks for whole minutes. Use consistent human units
   and maximum labels, with exact accounting values in secondary details. Keep
   concurrency, lifetime execution count, spending and retained-record/storage
   limits separate. Show usage/headroom for limits that actually reject work,
   including the independent 1,000 charged-job-record cap. This joins the existing
   admission and quota work rather than introducing another controls system.
   Evidence: [fields](https://github.com/schmitfe/compute-relay/blob/5ae8d589338ae93107fd72af0be7a07178cdc824/src/compute_relay/control/forms.py#L37),
   [allowance](https://github.com/schmitfe/compute-relay/blob/5ae8d589338ae93107fd72af0be7a07178cdc824/src/compute_relay/control/templates/broker/live_project_allowance.html),
   [cluster limits](https://github.com/schmitfe/compute-relay/blob/5ae8d589338ae93107fd72af0be7a07178cdc824/src/compute_relay/control/cluster_configuration.py#L28).

7. **Environment creation shows irrelevant and competing inputs.** With existing
   system Python selected, the rendered page still shows an environment prefix,
   Conda executable and detected selectors containing only their manual fallback.
   Show fields relevant to the selected kind, make detected versus manual input
   explicit and collapse overrides. Keep module ordering, stale-inspection
   validation and a usable no-JavaScript fallback. Rename "Environment prefix"
   to "Environment directory" and "Create immutable recipe" to "Save environment",
   while explaining that changes require a new recipe. Evidence:
   [fields and precedence](https://github.com/schmitfe/compute-relay/blob/5ae8d589338ae93107fd72af0be7a07178cdc824/src/compute_relay/control/forms.py#L131),
   [rendering](https://github.com/schmitfe/compute-relay/blob/5ae8d589338ae93107fd72af0be7a07178cdc824/src/compute_relay/control/templates/broker/environment_form.html#L16).

8. **Human operations stop short of cancellation and result recovery.** The
   review page offers decisions on pending requests and diagnostic logs, but no
   running-attempt cancellation, output-collection retry or declared-result
   retrieval action. These capabilities exist through authenticated project
   clients. Add state-specific human actions using the existing services, owner
   authorization, CSRF protection and safe retry identities. Distinguish compute
   outcome, final accounting, collection and local export. Collection retry must
   retain the same attempt and never resubmit compute; cancellation is not an
   immediate refund. A first result section can use the existing workspace
   helper instructions. Any browser download requires human authorization;
   bearer-only API links will not work with a session. Evidence:
   [review](https://github.com/schmitfe/compute-relay/blob/5ae8d589338ae93107fd72af0be7a07178cdc824/src/compute_relay/control/templates/broker/review.html),
   [routes](https://github.com/schmitfe/compute-relay/blob/5ae8d589338ae93107fd72af0be7a07178cdc824/src/compute_relay/control/urls.py),
   [collection recovery](https://github.com/schmitfe/compute-relay/blob/5ae8d589338ae93107fd72af0be7a07178cdc824/src/compute_relay/control/transfers.py#L86),
   [client retrieval](local-client-setup.md#initialize-a-science-project).

9. **Optional-module status and page structure need small corrections.** The
   environment-build page says ready environments always need explicit permission
   and shows **Permit this environment** for every published build, even though
   verified builds can be automatically permitted. Show the actual existing
   `permitted_for_jobs` state and only the applicable action. This is confirmed
   by the template/service contract, not a new end-to-end build. Storage already
   explicitly says its settings are advisory and do not redirect jobs; improve
   hierarchy and use "planning estimates" around its defaults rather than
   claiming a missing warning. Keep all optional modules. Add distinct page
   titles, a main landmark, current navigation state and accessible checkbox
   group names. Evidence: [build page](https://github.com/schmitfe/compute-relay/blob/5ae8d589338ae93107fd72af0be7a07178cdc824/src/compute_relay/extensions/environments/templates/broker_environments/builds.html#L4),
   [permission status](https://github.com/schmitfe/compute-relay/blob/5ae8d589338ae93107fd72af0be7a07178cdc824/src/compute_relay/extensions/environments/services.py#L87),
   [automatic permission](https://github.com/schmitfe/compute-relay/blob/5ae8d589338ae93107fd72af0be7a07178cdc824/src/compute_relay/extensions/environments/services.py#L347),
   [storage page](https://github.com/schmitfe/compute-relay/blob/5ae8d589338ae93107fd72af0be7a07178cdc824/src/compute_relay/extensions/storage/templates/broker_storage/project_storage.html),
   [shared layout](https://github.com/schmitfe/compute-relay/blob/5ae8d589338ae93107fd72af0be7a07178cdc824/src/compute_relay/control/templates/broker/base.html).

## Scope and acceptance

Start with operational visibility and comment preservation, then remove the two
redundant choices and improve setup/key handoffs. Grouping, units, environment
disclosure and module status follow within the same usability scope. Human
cancellation/collection recovery needs focused authorization and state-transition
tests. No new frontend framework, wizard state or second policy model is needed.

The real novice walkthrough should cover connecting, terminal authentication,
directory/limit confirmation, environment selection, a reviewed tiny test,
project grants, workspace initialization and verified result retrieval. The
experienced walkthrough should cover a second cluster, restricted key, a burst
exceeding 100 jobs, old running work, cancellation and failed collection.
Include keyboard-only validation, no-JavaScript behavior and live updates that
preserve drafts, settings and the selected job view. Unit conversions must not
increase authority through rounding.

Do not remove independent cluster/project/key authority, deny-all grants,
maximum resources, allowance, expiry, concurrency, lifetime accounting, exact
package approval or uncertainty about remote state. Environment retirement and
local/remote deletion stay in the existing retention work, with reference and
ownership checks; they are not a simple Delete button.

Six disposable validator probes passed, independently rerun by the lead. They
asserted current behavior for key-policy equivalence, new-project recipe choices,
edit-project round trips, old running-job visibility, failed-review comment loss
and advanced module errors. The last check rejected a suspected defect: advanced
module errors already open their disclosure. The probes demonstrate the findings,
not fixes. No application regression suite or real Slurm acceptance was needed
for this documentation-only review; both are required for the relevant fixes.

## Implemented after the review

The portal now shares 50-row job navigation across initial pages and live
updates, with All, Unresolved and Awaiting review filters. Stale decisions retain
the matching escaped comment. Human cancellation, same-attempt collection retry
and declared-result downloads reuse the existing durable services and verified
object reader. Cancellation creates no extra charged retry receipt, so exhausted
metadata cannot block stopping work. Disabling new submissions still allows
recovery of existing results through an owned, current connection.

Setup now groups project/key decisions, grants clusters and their environments
in one pass, hides irrelevant environment fields and discloses SSH overrides.
The Slurm-only selector and duplicate key confirmation choice are removed.
A small data migration maps the equivalent old key choice to Follow project
policy. No runtime compatibility mode was added. The key success page supplies
workspace initialization instructions; optional module permissions remain opt-in.

Compute allowance and reservations use hours with exact seconds available;
editable walltime and SSH lifetime use minutes. Project capacity shows pending
file claims, retained-record/metadata limits and installation headroom. Optional
build pages distinguish readiness from permission; storage remains explicitly
advisory. Shared titles, landmarks, checkbox groups and error targets improve
keyboard navigation. Polling preserves open diagnostic/capacity disclosures.

Project pages now share a named project breadcrumb, a direct return to the
project and shortcuts to its settings and agent keys. Optional tools live in
the same **Advanced project settings** disclosure on every project page,
including environment build permission, storage, workflows and investigations.
Nested forms retain their immediate parent in the breadcrumb. Cluster pages
use the matching cluster context and return pattern. Saving project settings
or changing/revoking a key returns to the relevant section instead of requiring
another scroll through job history. These links do not grant permissions or
change execution policy.

Navigation uses positive spacing instead of pulling into preceding metadata.
Project identity stays in its URL and authenticated API; the redundant UUID line
was removed from the heading. Job metadata uses its own line, related filter
controls stay together and long headings and optional-module tables wrap or
scroll within the page. Browser checks cover the populated project and build
permission pages at desktop and narrow widths.

Regression tests cover old unresolved jobs after a burst, live pagination,
escaped drafts, grants, authority/CSRF boundaries, quota-full cancellation,
recovery without another admission, verified downloads and minute conversion.
Synthetic real-HTTP reviewed/automatic workflows and clean-package installation
checks pass. A fresh human novice/expert walkthrough and real-site recovery
acceptance remain open; the original portal stall is a separate investigation.
