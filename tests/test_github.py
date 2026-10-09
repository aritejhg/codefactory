from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest

from codefactory.github import (
    API_ORIGIN,
    CIEvidence,
    CommitStatus,
    GitHubAPIError,
    GitHubClient,
    GitHubConfig,
    GitHubConfigurationError,
    verify_webhook,
)


TOKEN = "ghp_exampleToken123456789"
REPOSITORY = "acme/widget"
HEAD_SHA = "a" * 40


def make_config(**overrides: Any) -> GitHubConfig:
    values: dict[str, Any] = {
        "token": TOKEN,
        "allowed_repositories": frozenset({REPOSITORY}),
    }
    values.update(overrides)
    return GitHubConfig(**values)


def make_client(
    handler,
    config: GitHubConfig | None = None,
    *,
    token_provider=None,
) -> tuple[GitHubClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def capture(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    transport = httpx.MockTransport(capture)
    http_client = httpx.Client(base_url=API_ORIGIN, transport=transport)
    return (
        GitHubClient(
            config or make_config(),
            http_client=http_client,
            token_provider=token_provider,
        ),
        seen,
    )


def issue(number: int, **overrides: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "number": number,
        "title": f"Task {number}",
        "body": "Please implement this.",
        "state": "open",
        "labels": [{"name": "factory:ready"}],
        "html_url": f"https://github.com/{REPOSITORY}/issues/{number}",
        "updated_at": "2026-10-08T12:00:00Z",
    }
    value.update(overrides)
    return value


def pull_request(**overrides: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "number": 21,
        "title": "Add feature",
        "body": "Implementation evidence",
        "state": "open",
        "draft": False,
        "merged": False,
        "head": {"sha": HEAD_SHA, "ref": "factory/issue-17"},
        "base": {"ref": "main"},
        "html_url": f"https://github.com/{REPOSITORY}/pull/21",
        "merged_at": None,
    }
    value.update(overrides)
    return value


def test_config_prefers_environment_token_over_file_without_exposing_secrets(
    tmp_path: Path,
):
    token_file = tmp_path / "github-token"
    token_file.write_text("file-secret\n", encoding="utf-8")
    config = GitHubConfig.from_env(
        {
            "CODEFACTORY_GITHUB_TOKEN": " env-secret ",
            "CODEFACTORY_GITHUB_TOKEN_FILE": str(token_file),
            "CODEFACTORY_GITHUB_ALLOWED_REPOSITORIES": "Acme/Widget,other/repo",
            "CODEFACTORY_GITHUB_WEBHOOK_SECRET": " shared secret ",
        }
    )

    assert config.token == "env-secret"
    assert config.allowed_repositories == {"acme/widget", "other/repo"}
    assert config.admission_label == "factory:ready"
    assert "env-secret" not in repr(config)
    assert config.webhook_secret == " shared secret "
    assert " shared secret " not in repr(config)
    assert config.allows_repository("ACME/widget")


def test_config_reads_token_file_and_requires_explicit_allowlist(tmp_path: Path):
    token_file = tmp_path / "github-token"
    token_file.write_text("file-secret\n", encoding="utf-8")
    config = GitHubConfig.from_env(
        {
            "CODEFACTORY_GITHUB_TOKEN_FILE": str(token_file),
            "CODEFACTORY_GITHUB_ALLOWED_REPOSITORIES": REPOSITORY,
        }
    )
    assert config.token == "file-secret"

    with pytest.raises(GitHubConfigurationError, match="allowlist"):
        GitHubConfig.from_env({"CODEFACTORY_GITHUB_TOKEN": TOKEN})
    with pytest.raises(GitHubConfigurationError, match="invalid name"):
        make_config(allowed_repositories=frozenset({"https://bad.example/x/y"}))


def test_injected_token_provider_is_used_per_request():
    tokens = iter(["first-token", "second-token"])
    client, seen = make_client(
        lambda request: httpx.Response(200, json=issue(1)),
        make_config(token=None),
        token_provider=lambda: next(tokens),
    )
    client.get_issue(REPOSITORY, 1)
    client.get_issue(REPOSITORY, 1)

    assert [request.headers["authorization"] for request in seen] == [
        "Bearer first-token",
        "Bearer second-token",
    ]


def test_issue_listing_paginates_filters_prs_and_requires_admission_label():
    page_requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        query = dict(request.url.params)
        assert query["labels"] == "factory:ready"
        page_requests.append((request.url.path, query["page"]))
        if query["page"] == "1":
            items = [issue(1)]
            items.extend(issue(number, labels=[]) for number in range(2, 100))
            items.append(issue(100, pull_request={"url": "https://api.github.com/elsewhere"}))
            # A server-supplied next link must not be followed with credentials.
            return httpx.Response(
                200,
                json=items,
                headers={"Link": '<https://attacker.invalid/steal?page=2>; rel="next"'},
            )
        return httpx.Response(200, json=[issue(101)])

    client, seen = make_client(handler)
    results = client.list_admitted_issues(REPOSITORY)

    assert [entry.number for entry in results] == [1, 101]
    assert page_requests == [("/repos/acme/widget/issues", "1"), ("/repos/acme/widget/issues", "2")]
    assert all(request.url.host == "api.github.com" for request in seen)
    assert all(request.headers["authorization"] == f"Bearer {TOKEN}" for request in seen)


def test_repository_allowlist_is_checked_before_network_request():
    client, seen = make_client(lambda _request: httpx.Response(200, json=[]))

    with pytest.raises(ValueError, match="allowlist"):
        client.list_admitted_issues("someone/else")
    assert seen == []


def test_api_errors_do_not_include_token_or_response_body():
    secret_body = f"upstream response echoed {TOKEN}"
    client, _ = make_client(
        lambda _request: httpx.Response(403, text=secret_body)
    )

    with pytest.raises(GitHubAPIError) as error:
        client.get_issue(REPOSITORY, 1)

    assert error.value.status_code == 403
    assert TOKEN not in str(error.value)
    assert secret_body not in str(error.value)


def test_ci_evidence_is_pinned_to_current_pull_request_head():
    current_run = {
        "name": "unit tests",
        "status": "completed",
        "conclusion": "success",
        "head_sha": HEAD_SHA,
        "completed_at": "2026-10-08T12:30:00Z",
        "html_url": "https://github.com/acme/widget/actions/runs/1",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/pulls/21"):
            return httpx.Response(200, json=pull_request())
        if request.url.path.endswith(f"/commits/{HEAD_SHA}/status"):
            return httpx.Response(
                200,
                json={"state": "success", "statuses": [{"context": "build", "state": "success"}]},
            )
        if request.url.path.endswith(f"/commits/{HEAD_SHA}/check-runs"):
            return httpx.Response(
                200,
                json={"total_count": 1, "check_runs": [current_run]},
            )
        return httpx.Response(404)

    client, seen = make_client(handler)
    evidence = client.get_ci_evidence(REPOSITORY, 21)

    assert isinstance(evidence, CIEvidence)
    assert evidence.head_sha == HEAD_SHA
    assert evidence.successful
    assert [run.name for run in evidence.check_runs] == ["unit tests"]
    assert all(HEAD_SHA in request.url.path for request in seen[1:])


@pytest.mark.parametrize("endpoint,item", [
    ("status", None),
    ("status", {"context": None, "state": "pending"}),
    ("status", {"context": "build", "state": "unknown"}),
    ("check-runs", None),
    ("check-runs", {"name": "tests", "status": "completed", "conclusion": "success", "head_sha": "b" * 40}),
    ("check-runs", {"name": "tests", "head_sha": HEAD_SHA}),
])
def test_anomalous_ci_evidence_fails_closed(endpoint, item):
    def handler(request: httpx.Request) -> httpx.Response:
        if endpoint == "status":
            return httpx.Response(200, json={"state": "pending", "statuses": [item]})
        return httpx.Response(200, json={"check_runs": [item]})

    client, _ = make_client(handler)
    with pytest.raises(GitHubAPIError, match="evidence"):
        if endpoint == "status":
            client.get_commit_status(REPOSITORY, HEAD_SHA)
        else:
            client.get_check_runs(REPOSITORY, HEAD_SHA)


def test_ci_evidence_fails_if_current_check_is_pending_or_failed():
    pending_run = {
        "name": "integration",
        "status": "in_progress",
        "conclusion": None,
        "head_sha": HEAD_SHA,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/pulls/21"):
            return httpx.Response(200, json=pull_request())
        if request.url.path.endswith("/status"):
            return httpx.Response(200, json={"state": "success", "statuses": []})
        return httpx.Response(200, json={"check_runs": [pending_run]})

    client, _ = make_client(handler)
    assert not client.get_ci_evidence(REPOSITORY, 21).successful


def test_actions_only_pending_combined_status_passes_with_successful_current_checks():
    successful_run = {
        "name": "unit tests",
        "status": "completed",
        "conclusion": "success",
        "head_sha": HEAD_SHA,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/pulls/21"):
            return httpx.Response(200, json=pull_request())
        if request.url.path.endswith("/status"):
            return httpx.Response(200, json={"state": "pending", "statuses": []})
        return httpx.Response(200, json={"check_runs": [successful_run]})

    client, _ = make_client(handler)
    assert client.get_ci_evidence(REPOSITORY, 21).successful


def test_ci_without_any_legacy_context_or_check_run_never_passes():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/pulls/21"):
            return httpx.Response(200, json=pull_request())
        if request.url.path.endswith("/status"):
            return httpx.Response(200, json={"state": "success", "statuses": []})
        return httpx.Response(200, json={"check_runs": []})

    client, _ = make_client(handler)
    assert not client.get_ci_evidence(REPOSITORY, 21).successful


def test_issue_comment_read_and_write_support_plan_approval_threads():
    requests: list[tuple[str, str, dict[str, Any] | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        requests.append((request.method, request.url.path, body))
        if request.method == "GET":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 55,
                        "body": "Approved plan",
                        "created_at": "2026-10-08T13:00:00Z",
                        "updated_at": "2026-10-08T13:00:00Z",
                        "html_url": "https://github.com/acme/widget/issues/17#issuecomment-55",
                        "user": {"login": "maintainer"},
                    }
                ],
            )
        return httpx.Response(
            201,
            json={
                "id": 56,
                "body": "Plan v1 is ready for your approval.",
                "user": {"login": "factory-bot"},
            },
        )

    client, _ = make_client(handler)
    comments = client.list_issue_comments(REPOSITORY, 17)
    created = client.create_issue_comment(
        REPOSITORY, 17, body="Plan v1 is ready for your approval."
    )

    assert comments[0].author == "maintainer"
    assert comments[0].body == "Approved plan"
    assert created.comment_id == 56
    assert requests[1] == (
        "POST",
        "/repos/acme/widget/issues/17/comments",
        {"body": "Plan v1 is ready for your approval."},
    )


def test_draft_review_and_merge_operations_use_validated_api_payloads():
    requests: list[tuple[str, str, dict[str, Any] | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        requests.append((request.method, request.url.path, body))
        if request.method == "POST" and request.url.path.endswith("/pulls"):
            return httpx.Response(201, json=pull_request(draft=True))
        if request.method == "POST" and request.url.path.endswith("/reviews"):
            return httpx.Response(
                200,
                json={
                    "state": "COMMENTED",
                    "commit_id": HEAD_SHA,
                    "body": "Looks good overall.",
                    "submitted_at": "2026-10-08T13:00:00Z",
                    "user": {"login": "reviewer"},
                },
            )
        if request.method == "PUT":
            return httpx.Response(200, json={"merged": True, "message": "Pull Request successfully merged", "sha": "c" * 40})
        return httpx.Response(404)

    client, _ = make_client(handler)
    created = client.create_draft_pull_request(
        REPOSITORY, title="Add feature", head="factory/issue-17", base="main", body="Evidence"
    )
    review = client.submit_pull_request_review(
        REPOSITORY, created.number, event="COMMENT", body="Looks good overall.", commit_id=HEAD_SHA
    )
    merge = client.merge_pull_request(
        REPOSITORY, created.number, expected_head_sha=HEAD_SHA, merge_method="squash"
    )

    assert created.draft
    assert review.reviewer == "reviewer"
    assert review.state == "COMMENTED"
    assert merge.merged and merge.merge_commit_sha == "c" * 40
    assert requests[0][2] == {
        "title": "Add feature",
        "head": "factory/issue-17",
        "base": "main",
        "body": "Evidence",
        "draft": True,
    }
    assert requests[1][2] == {"event": "COMMENT", "body": "Looks good overall.", "commit_id": HEAD_SHA}
    assert requests[2][2] == {"sha": HEAD_SHA, "merge_method": "squash"}


def test_webhook_verification_authenticates_raw_bytes_and_validates_headers():
    secret = " shared secret "
    body = b'{"action":"opened","issue":{"number":17}}'
    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    delivery_id = str(uuid.uuid4())

    webhook = verify_webhook(
        secret=secret,
        raw_body=body,
        signature=signature,
        event="issues",
        delivery_id=delivery_id,
    )
    assert webhook.delivery_id == delivery_id
    assert webhook.event == "issues"
    assert webhook.payload["action"] == "opened"

    cases = [
        {"signature": "sha256=" + "0" * 64},
        {"event": ""},
        {"event": "bad event"},
        {"delivery_id": "not-a-uuid"},
        {"raw_body": b"[]"},
        {"raw_body": b"not-json"},
    ]
    args: dict[str, Any] = {
        "secret": secret,
        "raw_body": body,
        "signature": signature,
        "event": "issues",
        "delivery_id": delivery_id,
    }
    for changes in cases:
        invalid = {**args, **changes}
        if "raw_body" in changes:
            invalid["signature"] = "sha256=" + hmac.new(secret.encode(), invalid["raw_body"], hashlib.sha256).hexdigest()
        with pytest.raises(ValueError, match="invalid GitHub webhook payload" if "raw_body" in changes else None):
            verify_webhook(**invalid)


def test_injected_client_cannot_send_credentials_to_arbitrary_origin():
    config = make_config()
    unsafe = httpx.Client(base_url="https://attacker.invalid")
    try:
        with pytest.raises(GitHubConfigurationError, match="GitHub API origin"):
            GitHubClient(config, http_client=unsafe)
    finally:
        unsafe.close()


@pytest.mark.parametrize('labels,accepted', [([], False), ([{'name': 'other'}], False), ([{'name': 'FACTORY:READY'}], True)])
def test_single_issue_fetch_requires_configured_admission_label(labels, accepted):
    client, _ = make_client(lambda _request: httpx.Response(200, json=issue(17, labels=labels)))
    if accepted:
        assert client.get_issue(REPOSITORY, 17).number == 17
    else:
        with pytest.raises(GitHubAPIError, match='admission label'):
            client.get_issue(REPOSITORY, 17)


@pytest.mark.parametrize("item", [None, "malformed", 17])
def test_issue_comments_reject_malformed_entries(item):
    client, _ = make_client(lambda _request: httpx.Response(200, json=[item]))
    with pytest.raises(GitHubAPIError, match="invalid issue comments"):
        client.list_issue_comments(REPOSITORY, 17)


@pytest.mark.parametrize("method,error", [("list_admitted_issues", "invalid issue list"), ("list_pull_request_reviews", "invalid review data")])
def test_admission_and_review_lists_reject_malformed_evidence(method, error):
    client, _ = make_client(lambda _request: httpx.Response(200, json=[None]))
    with pytest.raises(GitHubAPIError, match=error):
        getattr(client, method)(REPOSITORY, 17) if method == "list_pull_request_reviews" else getattr(client, method)(REPOSITORY)


@pytest.mark.parametrize("context_state", ["pending", "failure", "error"])
def test_combined_success_cannot_override_nonpassing_context(context_state):
    evidence = CIEvidence(REPOSITORY, 17, HEAD_SHA, CommitStatus("success", (("build", context_state),)), ())
    assert not evidence.successful
