# Local recovery runtime

The VM runs the authenticated controller at `http://127.0.0.1:8000` under the
user's `codefactory.service`, with restart-on-failure supervision. Its persistent
database is `~/.local/state/codefactory/controller.sqlite3`; the older demo
database is preserved. The service uses this entrypoint:

```sh
uv sync --locked --group dev
.venv/bin/uvicorn codefactory.runtime:create_runtime_app --factory --host 127.0.0.1 --port 8000
```

Set `CODEFACTORY_DB_PATH`, `CODEFACTORY_AUTH_TOKENS_FILE`, and optionally
`CODEFACTORY_CONFIG` in the service environment. The existing operator mapping
is a user-owned mode-0600 JSON token-to-principal object at
`~/.config/codefactory/operator-tokens.json`. Task routes require its bearer
token; public health contains no credential values. Keep credentials and the
database outside every agent worktree. Never print tokens in terminal examples.

```sh
systemctl --user status codefactory.service
curl --fail --silent http://127.0.0.1:8000/health
systemctl --user restart codefactory.service
journalctl --user -u codefactory.service -n 30 --no-pager
```

`config/runtime.yaml` selects native providers and models: Luna
`gpt-5.6-luna` with `xhigh` for triage/CI, Sol `gpt-6.1-sol` with `xhigh` for
planning/implementation, and the advertised Claude native `opus` ID for review.
The Opus ID makes no fixed-version claim. An absent requested model blocks its
role; the runtime never selects a fallback or paid API key. Codex ACP is pinned
to `@agentclientprotocol/codex-acp@2.1.1`. The current VM's isolated Claude
adapter install is pinned to `@zed-industries/claude-agent-acp@0.23.1` under
`~/.local/share/codefactory/adapters`; `adapters` can override executable argv
in YAML without changing global agent configuration.

The shared `base_prompt_file` resolves from the controller checkout, not task
content. Its trusted body is prepended to every role prompt. Claude review has
all native tools removed and receives bounded controller-measured plan/diff
context. Each task gets its own controller-created Git worktree, persisted base
SHA, branch, and native session. A separate policy verifies trust, state, role,
session identity, and exact worktree before a prompt. Codex receives a stripped
environment and native filesystem restrictions; controller credentials remain
outside the workspace. A nonsecret outside-workspace canary was denied during
native recovery verification.

`/health` distinguishes the running API/scheduler from blocked integrations.
The factory GitHub bootstrap file is `~/.config/codefactory/bootstrap/github.token`;
its missing/empty content blocks GitHub reconciliation. Authorized connector
snapshots can be explicitly admitted by the local operator without fabricating
GitHub labels. A snapshot must be user-owned mode 0600, outside agent checkouts,
with `repository`, `number`, `title`, `body`, actual `labels`, and the exact
`source_url` for the existing allowlisted issue:

```sh
uv run python -m codefactory.operator import-github \
  ~/.local/state/codefactory/issue24-bootstrap.json --operator-admit \
  --db ~/.local/state/codefactory/controller.sqlite3
```

`--operator-admit` is a privileged, audited local bootstrap action independent
of the GitHub admission label. It is never an HTTP caller-supplied trust flag.
Omit it to require the genuine configured label. A harmless triage smoke uses a
new separate database and cancels after the one triage turn. Choose an unused
database path for each smoke:

```sh
uv run python -m codefactory.operator triage-smoke \
  ~/.local/state/codefactory/issue24-bootstrap.json --operator-admit \
  --db ~/.local/state/codefactory/native-smoke-new.sqlite3
```

Native quota refusals persist a per-task/per-role `retry_after` cooldown (default
900 seconds), preserve the native session/worktree/state, and retry the same
route after the cooldown without consuming ordinary failure attempts. This is
a local retry schedule, not a claimed provider reset time. Normal outcome JSON
that discusses quotas is processed normally. Human questions park tasks in
`WAITING_FOR_USER`; authorized answers resume the saved role. Cancelled tasks
stay cancelled across service restarts.

This recovery enables real read-only triage/planning/review dispatch. BUILD/CI
execution remains explicitly held until the reviewed foundation is enabled.
CI Luna only diagnoses/reports and delegates every fix to Sol, followed by Opus
review; legacy `narrow_fix` output is converted to delegation. Local preview
supervision, Projects synchronization, complete LLM/MCP access gateways, and
automated publication/bot readiness gates are pending. Cloud ingress has
separate credential and verified-$0 prerequisites. Remote mutations remain
disabled in this composition; human plan/PR approval gates remain durable.
Every PR stays draft until both Greptile and CodeRabbit pass the current head
and all findings are resolved; readiness never substitutes for human merge
authorization.

Recovery validation uses the existing API/workflow/scheduler/ACP checks, one
meaningful quota/restart regression, and real native smoke/review. A completed
triage turn or a running health endpoint does not claim complete issue-to-PR
delivery.

`max_concurrent_agents: 5` and `max_open_prs: 5` configure global native-turn and feature/PR capacity (strict integers 1–100). Dispatch remains serial. Active PRs include drafts and ready PRs; merge/closure frees their slot. Unpublished features reserve one slot across roles/retries/waits. At full PR capacity, only verified linked active-PR work runs; new features remain queued before triage/planning/building. All controllers must share the database and configured limits. `/health.capacity` exposes counts and block reasons; unknown/stale inventory fails closed and bootstrap publication remains disabled. Native-started leases cannot be reaped by expiry alone: abrupt restart or uncertain transport requires operator-verified native shutdown, while graceful scheduler stop releases its own claims after adapter shutdown.
