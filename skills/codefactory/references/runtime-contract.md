# Runtime contract

This is an implementation guide for the phase-one bootstrap, not a promise that every PRD feature is enabled. Verify the checked-out source and runtime health before acting. Repository-relative sources are `config/runtime.yaml` and `codefactory/{runtime,workflow,scheduler,app,native,roles,workspaces,secrets,cloud}.py`; `PRD.md` and `ARCHITECTURE.md` describe the target. Source/runtime changes require updating this guide when behavior changes.

## State and evidence

The intended path is `TRIAGE → PLAN → PLAN_REVIEW → HUMAN_PLAN_APPROVAL → BUILD → CODE_REVIEW → CI → PREVIEW_READY → HUMAN_PR_APPROVAL → MERGED`, with reviewed repairs/replanning and `WAITING_FOR_USER`, `BLOCKED`, or `CANCELLED` branches. `WorkflowService` alone changes application workflow state. An agent outcome is a request for deterministic validation, not transition authority.

Plan submission increments the version and clears its prior review/approval. Plan review and human approval must identify that version. Human approval is accepted through the owner-authorized controller endpoint, not inferred from a GitHub comment or generic review. A measured new commit requires current human plan approval; it clears code review, CI, preview references/health, and prior PR approvals. Reviewer, CI, preview, and human PR approval evidence must all match the current head. The current workflow requires a healthy current-head preview for the human PR approval path; it has no implemented non-runnable-project exemption.

The desired issue #27 readiness gate additionally requires complete passing Greptile and CodeRabbit reviews, with all findings resolved on the current head. The bootstrap does not yet enforce or publish that gate. Do not promote a draft through a lower-level path that omits it. A ready PR still needs explicit human head-specific approval and an authorized merge action; controller-confirmed GitHub merge evidence establishes `MERGED`.

## Role outcome contract

The scheduler supplies the schema for the actual turn. These shapes document the current contract; use real text/values, not the alternatives as literal values.

| Role/state | Accepted outcome |
| --- | --- |
| Triage | `decision`: `ready`, `needs_user`, or `blocked`; nonblank `summary`; `risk`: `low`, `medium`, or `high`; integer `priority` from -100 to 100; `questions` and `dependencies` arrays |
| Planner | Nonblank `plan` and `questions` array; a question does not remove the nonblank-plan requirement |
| Reviewer / PLAN_REVIEW | `decision`: `approve`, `revise`, or `needs_user`; `findings` and `questions` arrays |
| Reviewer / CODE_REVIEW | `decision`: `approve`, `changes_requested`, `replan`, or `needs_user`; `findings` and `questions` arrays |
| Implementer | `decision`: `done`, `continue`, or `needs_user`; `summary` and `questions` array |
| CI babysitter | `action`: `delegate_to_implementer` or `no_safe_fix`; text `summary`. Legacy `narrow_fix` output is coerced to delegation. |

`needs_user` requires an actual question. Findings/questions are strings, not objects. The controller measures an implementer's clean new commit; `done` cannot claim a head SHA or bypass that measurement. Under issue #26, Luna diagnoses/reports and delegates every code/configuration/test fix to Sol, followed by Opus review. The scheduler supplies a read-only CI schema and converts legacy `narrow_fix` to `delegate_to_implementer` without measuring a babysitter commit. The workflow service's legacy accepted action also returns work to the implementer. Triage's `dependencies` array is presently advisory: `set_triage` persists priority and audit data but does not enforce dependency admission. Do not represent dependency scheduling as operational.

## Workspaces and authorization

`TaskWorkspaces` admits allowlisted trusted repositories and creates a controller-owned `factory/task-<id>-<run_id>` Git worktree with a persisted baseline. It checks the Git common directory, branch identity, non-symlink root, and session cwd. An existing unregistered worktree or missing persisted worktree requires operator recovery. It does not implement automatic stacked-branch construction; identify stack dependencies/bases explicitly without claiming that feature exists.

The turn's separate access policy verifies role/state/task/session/worktree. Native read-only roles are triage/planner/reviewer/CI babysitter; only implementer routing requests workspace-write. Default runtime still denies implementer and CI turns and has no commit reader. Claude supports only explicit model, tool-free reviewer sessions, with empty tools/MCP/plugins/hooks/settings sources. Current native Claude model IDs are `default`, `opus`, and `haiku`; the YAML selects `opus` for review, without claiming a fixed version. Verify the adapter's advertised IDs when changing routes. No role silently falls back when its native adapter/model configuration is unavailable.

The controller reads `base_prompt_file` (default `skills/codefactory/SKILL.md`) from its source checkout or the `CODEFACTORY_BASE_PROMPT_FILE` override and injects that trusted text before each role's instructions/schema/context. A missing, empty, or oversized shared prompt blocks authorization; do not assume a task worktree contains the current skill or ask a tool-free reviewer to open it.

## Durable waits, cancellation, and recovery

- Questions persist `waiting_role` and `waiting_state`. The authenticated owner message API resumes that stored state and queues the answer for the same role; eligible verified GitHub comments use the same mechanism. A message is neither a plan nor PR approval. GitHub reconciliation requires working factory credentials.
- Owner cancellation sets `CANCELLED`, clears waiting metadata, interrupts claimed turns, and requests cancellation of active native sessions. No automatic reopening/resume-from-cancel endpoint exists. Keep work/session artifacts unless separately authorized to remove them.
- Startup and turn claiming mark expired leases interrupted. Unexpired leases still prevent duplicate claims. Sessions persist ID, cwd, native state directory, model configuration, and capabilities; subsequent turns load the saved session when necessary. Missing native state or changed configuration is a blocker, not permission to restart from scratch.
- Native usage/rate/quota errors mark the turn interrupted and record `role_pauses(task_id, role, retry_after, reason)` with `native_quota_wait`. The cooldown defaults to 900 seconds (minimum configured 60); it is not a verified provider reset time. State, queued messages, native session, and worktree remain available for another eligible turn; quota errors do not increment normal failures.
- Ordinary turn failures increment a bounded per-role counter. Current defaults allow two plan revisions, three review repairs, two CI fixes, and three turn failures before further failure blocks work. There is no general retry-counter reset API, manual immediate-quota-resume API, guaranteed checkpoint/snapshot service, or independent role-library installation in this package.

## Integration limits

The local authenticated API can run while ingestion is blocked. Manual task creation remains untrusted until verified admission. The controller's private GitHub credential is absent/empty in this bootstrap; a user's unrelated CLI login is not factory authorization. Projects synchronization, local preview runner, full LLM/MCP gateway policy, and automated remote draft/bot readiness publishing remain pending. Read-only native roles are usable only when their configured adapter/model is available and their task is admitted; “ready” adapter health alone does not admit a task.

Cloudflare verification/vault clients do not imply deployed cloud resources or issued agent grants. Their live hard-$0 gate blocks operations when free entitlement or fresh account-wide usage/headroom cannot be verified. Secrets use exact scope and expiration; control-plane clients/credentials are never passed to agents. Local VM execution/previews have separate trust and capacity requirements from optional Cloudflare ingress.
