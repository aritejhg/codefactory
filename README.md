# Codefactory

Codefactory is a small, single-operator task API baseline. It stores tasks and state changes in SQLite. The server is for loopback use; the example `TRIAGE` → `PLAN` transition is a manual operator action, and tasks marked `trusted=false` remain in `TRIAGE`. This is a foundation example, not a Phase 1-complete factory: GitHub reconciliation, ACP sessions, and automatic agents are not implemented. Work tracks [foundation](https://github.com/aritejhg/codefactory/issues/1) and [verification](https://github.com/aritejhg/codefactory/issues/2); [labeled GitHub issue admission](https://github.com/aritejhg/codefactory/issues/3) is planned after the baseline.

## Local setup

Install [uv](https://docs.astral.sh/uv/), then install the locked dependencies and run the tests:

```sh
uv sync --locked --group dev
uv run pytest -q
```

Start the API on `127.0.0.1:8000`:

```sh
uv run uvicorn codefactory.app:create_app --factory --reload --host 127.0.0.1 --port 8000
```

Check that it is responding:

```sh
curl -sS http://127.0.0.1:8000/health
```

The SQLite database is `codefactory.sqlite3` in the current working directory by default. Set `CODEFACTORY_DB_PATH` to choose another file, for example `CODEFACTORY_DB_PATH=/var/lib/codefactory/tasks.sqlite3`. Keep that file on persistent storage to retain tasks across restarts.

## Create and advance a task

Create a task for GitHub issue 1 in `aritejhg/codefactory`:

```sh
curl -sS -X POST http://127.0.0.1:8000/tasks \
  -H 'Content-Type: application/json' \
  -d '{"repository":"aritejhg/codefactory","issue_number":1,"title":"baseline","trusted":true}'
```

Replace `<task-id>` with the returned task ID, then explicitly advance it from `TRIAGE` to `PLAN`:

```sh
curl -sS -X POST 'http://127.0.0.1:8000/tasks/<task-id>/transition' \
  -H 'Content-Type: application/json' \
  -d '{"expected_state":"TRIAGE","state":"PLAN"}'
```
