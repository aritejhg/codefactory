# Software Factory — Architecture

**Principle:** One deterministic controller; agents communicate through durable messages and native ACP session resumption. Keep GitHub, ingress, and telemetry in managed cloud; keep trusted execution and long-lived session storage on the existing Linux machine.

## Phase 1 — “Skunks with batteries”

```text
GitHub Issues/Projects/PRs/Actions  (canonical tasks, CI, approvals)
          │ webhooks + reconciler
          ▼
Linux VM: FastAPI controller + SQLite event log/queue + scheduler
          ├── ACP session gateway → Planner / Implementer / Reviewer
          │                        ↕ typed messages / resumption
          ├── Triage ACP agent (every admitted issue)
          ├── CI Babysitter ACP agent (on failed checks)
          ├── Sandbox manager → trusted isolated per-task workspaces
          │                     └── per-PR frontend/backend + health checks
          ├── Web dashboard + authenticated live ACP event stream
          └── OTel SDK/Collector → hosted Grafana Cloud (free limits)
          ▲
 Cloudflare Tunnel + Access (protected UI/API/preview ingress)
```

**Suggested open-source building blocks:** `skorokithakis/symphony` for orchestration ideas and session continuity (expect material changes to support multiple roles), `openai/symphony` for reconciliation/worker patterns, `addyosmani/factory` for repo policy/gates, `agentclientprotocol/agent-client-protocol` plus compatible ACP adapters (evaluate `openclaw/acpx`), OpenTelemetry Collector, FastAPI + SQLite, GitHub APIs, Docker Compose for **trusted** workload previews, and Cloudflare Tunnel. Evaluate `fabro-sh/fabro` as an alternative if graph/retry implementation becomes too complex. These are candidate components, not guaranteed drop-in compatibility.

**State split:** GitHub is authoritative for issue/PR status, priority and approvals; SQLite is authoritative for run attempts, event/message log, leases, role→session IDs, plan versions, review findings, retry counts, resource reservations, and sandbox/preview IDs. Agent session directories and worktrees persist on disk with off-machine backups. OTel is for diagnosis, **not** workflow state.

**Workflow/state:** `TRIAGE → PLAN → PLAN_REVIEW ↔ PLAN → HUMAN_PLAN_APPROVAL → BUILD ↔ CODE_REVIEW → CI → PREVIEW_READY → HUMAN_PR_APPROVAL → MERGED`. Implementer/reviewer may emit `REPLAN_REQUESTED` to resume Planner; material plan change invalidates prior approval. User questions park tasks in `WAITING_FOR_USER` without consuming agent slots. Failed CI invokes Babysitter; substantive fixes resume Implementer, then CI and Reviewer. Each transition is idempotent, leased, logged and tied to a plan version/commit SHA. Enforce bounded retries, timeout and spend/resource ceilings.

**Session viewing:** The server owns each ACP connection/session. Browsers connect via authenticated WebSocket/SSE for live updates and replay. Queue = next turn; steer = real-time **only when supported by the specific ACP agent**; interrupt = cancel active turn and resume the same session. Per-task ownership/authorization is checked for every read, message and control operation. Session IDs alone are not durable state: persist native session files and restore paths; verify resume after process/host restart.

**Admission and capacity (starting defaults, adjustable in config):** 2 active agent turns, 1 writing agent per branch, up to 3 active sandbox/workspaces, up to 2 simultaneous full-stack previews, warm compute pool 0, 3 code-review repairs, 2 plan-revisions, 2 CI fixes. Enforce total RAM/CPU/disk limits and backpressure. A live preview consumes capacity even if its agent session is idle; queued work waits. Agent slots and preview slots are separate. A PR cannot claim `PREVIEW_READY` unless the preview matches its current commit and health checks pass.

**Security boundary:** On a 4–8-core/8–16-GiB shared Linux VM, container isolation alone is **not** a sufficient untrusted-code boundary. Untrusted work must never receive host Docker socket, privileged mounts, production secrets, broad network access, or control-plane credentials. Determine KVM availability (`test -e /dev/kvm`) but **do not make KVM mandatory**: a verified independent isolation worker/provider is an alternative. At $0 cloud overflow budget, untrusted tasks remain blocked/queued when no safe free worker exists. Isolate preview applications and synthetic data; enforce Cloudflare Access and GitHub webhook verification.

## Phase 2 — Nice-to-haves / expansion

- Additional isolated worker nodes/cloud compute selected behind a `SandboxProvider` interface; optional Firecracker/E2B Embed when KVM exists; autoscaling, warm pools, suspension and snapshot restore.
- Configurable budget service for paid overflow and API inference (default disabled/zero), alerts and cost attribution; configurable per-agent model/provider and permissions.
- MCP gateway/tool catalog and an LLM gateway for API-key routes; keep subscription-authenticated agents on their permitted ACP routes. Add policy enforcement and centralized audit at both edges.
- Stale-preview archiving, pinning and restoration-on-visit via a stable gateway; preview TTL and resource priority policies.
- Advanced dashboard controls (RBAC, queue editing, analytics, session replay) and configurable deployment/approval policies; human production approval by default.

**Deployment order:** 1) verify host capacity/isolation and GitHub auth; 2) controller + SQLite + GitHub queue + ACP sessions; 3) triage/planning/review loops and human questions; 4) trusted sandbox + per-PR preview + CI babysitter; 5) OTel, secured viewer/dashboard, backups and restart/failure tests. Add Phase 2 only when demand or measured constraints justify it.

**Architecture decision gate:** Before implementing untrusted execution or automatic paid scaling, prove safe isolation and budget enforcement in tests. If the proposed Symphony fork requires rewriting most of its core, keep its patterns but implement the small controller directly rather than forcing the dependency.
