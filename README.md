# Codefactory

Codefactory is an authenticated local workflow controller with durable SQLite
tasks, approvals, native ACP sessions, and a scheduler. The recovery runtime
supports real native read-only triage/planning/review dispatch with isolated
task worktrees and configurable model routing. Missing integrations and the
remaining build/preview/publication gates are explicit in `/health`. See
[runtime operation and current limits](docs/runtime.md).

## Local setup

Install [uv](https://docs.astral.sh/uv/), then install the locked dependencies and run the tests:

```sh
uv sync --locked --group dev
uv run pytest -q
```

Start the API on `127.0.0.1:8000`:

```sh
uv run uvicorn codefactory.runtime:create_runtime_app --factory --host 127.0.0.1 --port 8000
```

Check that it is responding:

```sh
curl -sS http://127.0.0.1:8000/health
```

The runtime database defaults to `~/.local/state/codefactory/controller.sqlite3`.
Set `CODEFACTORY_DB_PATH` to choose another persistent file. Configure a protected
operator token mapping before using task routes.

## Create a manual task

Create a task for GitHub issue 1 in `aritejhg/codefactory`:

```sh
curl -sS -X POST http://127.0.0.1:8000/tasks \
  -H 'Authorization: Bearer <operator-token>' \
  -H 'Content-Type: application/json' \
  -d '{"repository":"aritejhg/codefactory","issue_number":1,"title":"baseline"}'
```

Manual tasks remain untrusted in `TRIAGE`. Trusted admission comes from verified
allowlisted GitHub input or the explicit local operator bootstrap command;
there is no generic transition endpoint or caller-supplied trust flag.
