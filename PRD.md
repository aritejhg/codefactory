# Software Factory — PRD

**Goal:** Deliver useful, tested software from GitHub issues to merged PRs with minimal infrastructure, human effort, and cost. Support existing repos and greenfield projects, including custom Docker Compose stacks. Target **20+ tasks/day** through queueing and eventual scale-out; throughput is a target, not a single-VM guarantee.

## Phase 1 — “Skunks with batteries” (small, end-to-end, usable)

- **GitHub is the work surface:** Issues/Projects own task state and priority; PRs own review/merge. A small web dashboard shows Kanban, running/waiting agents, live ACP sessions, PRs, previews, queue depth, CPU/RAM, and basic usage. External submitters see only their own permitted tasks/previews.
- **Four agent roles:** Triage every new issue (readiness, scope, risk, dependencies, proposed priority; human may override); Planner; Implementer; Reviewer (reviews both plans and code); plus an **event-driven CI Babysitter** that fixes narrowly scoped CI/config faults or delegates substantive changes back to the implementer's existing session.
- **Collaborative, resumable sessions:** Each role has a distinct persistent **ACP** session per task/PR. Planner ↔ Reviewer iterate until plan passes; human approves the initial plan and material revisions. Implementer ↔ Reviewer iterate on code; either can request replanning. The orchestrator routes typed messages, enforces limits, resumes existing sessions, and never relies on an LLM to manage state.
- **Human-in-the-loop:** Planner can conduct a “grill-me” clarification conversation through GitHub comments and the web session viewer; all answers sync to the same session. Any agent may ask operational questions; scope changes route to Planner and human approval. Users can **observe, queue a message, steer if the adapter supports it, or interrupt** an authorized ACP session.
- **Delivery gate:** Isolated workspace; deterministic tests/linters/build; independent code review; GitHub CI; bounded repair cycles; draft PR with evidence, trace/run ID, and a live frontend/backend preview **whenever the repo can expose a runnable service**. Human approves and merges. **Done means merged**, not merely PR opened.
- **Capacity/safety:** Global and per-repo queues; priority + dependencies + aging; admission limits for concurrent agent turns, workspaces, CPU/RAM, and live previews. Preserve active previews until PR close/merge by default; queue work when capacity is full. Existing 4–8-core/8–16-GiB Linux host executes **trusted workloads only** unless sufficiently strong isolation is verified. Untrusted/external code must run on a separately isolated worker; with a **$0 incremental-cloud-spend default**, queue it if no safe free capacity exists. No insecure fallback.
- **Observability:** OTel traces, metrics, structured logs for controller, ACP turns, CI, sandbox lifecycle, previews, and cost/usage. Audit all approvals, messages, and handoffs. Keep sensitive prompts/tokens/secrets out of logs by default.

**Phase 1 acceptance:** One trusted existing repo and one greenfield demo complete triage → clarification → approved/revised plan → implementation → review/fix loop → CI repair → live preview → approved merged PR; planner and implementer sessions resume after restart; another user cannot inspect private task/session data; unsafe untrusted work remains queued; dashboard and OTel expose run state.

## Phase 2 — Nice-to-haves / scale and policy flexibility

- Configurable monthly/per-task cloud and inference spending, overflow provisioning, model/provider selection, and role-specific budgets; **no autonomous paid overflow until explicitly enabled**.
- Validated stronger sandbox isolation (microVM if available, or independent secure worker) for untrusted code; additional worker nodes, warm pools, autoscaling, quotas, and startup optimizations.
- MCP gateway/tool registry and LLM gateway for API-based models; subscription-backed agents remain behind ACP adapters, not forced through an incompatible inference proxy.
- Automatic stale-preview archive/restore-on-visit, pinned preview protections, policies for idle compute, extended preview capacity.
- Rich dashboard (queue reordering, budget controls, session replay/search, per-project RBAC, traces, agent analytics); configurable deployment policy, with **explicit human production approval as the default**.
- Advanced triage/dependency recommendations, richer recovery, multi-tenant controls, and provider-independent sandbox snapshots.

## Non-goals

No Kubernetes, agent supervisor LLM, custom vector database, or production auto-deploy in Phase 1. No promise that 20+ daily tasks or arbitrary simultaneous preview stacks fit on one modest host.
