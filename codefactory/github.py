"""Small, credential-safe GitHub REST client for the controller.

All requests go to the fixed github.com API origin. Repository names and API
resource identifiers are validated before they are used in a path. Callers can
inject an ``httpx.Client`` for tests and a token provider for short-lived
credentials such as GitHub App installation tokens.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

import httpx


API_ORIGIN = "https://api.github.com"
API_VERSION = "2022-11-28"
DEFAULT_ADMISSION_LABEL = "factory:ready"
_REPOSITORY_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
_SHA_RE = re.compile(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})\Z")
_EVENT_RE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_SIGNATURE_RE = re.compile(r"sha256=[0-9a-fA-F]{64}\Z")


class GitHubConfigurationError(ValueError):
    """The local GitHub integration configuration is incomplete or invalid."""


class GitHubAPIError(RuntimeError):
    """A sanitized GitHub API error that never includes response or token data."""

    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class GitHubConfig:
    """Runtime credentials and the repositories eligible for admission.

    ``CODEFACTORY_GITHUB_TOKEN`` takes precedence over
    ``CODEFACTORY_GITHUB_TOKEN_FILE``. The token is excluded from repr output.
    Repositories in ``allowed_repositories`` are the explicitly trusted repo
    allowlist; an issue also needs the configured admission label.
    """

    token: str | None = field(default=None, repr=False)
    allowed_repositories: frozenset[str] = frozenset()
    admission_label: str = DEFAULT_ADMISSION_LABEL
    webhook_secret: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.token is not None:
            object.__setattr__(self, "token", _clean_secret(self.token, "GitHub token"))
        if self.webhook_secret is not None:
            object.__setattr__(
                self,
                "webhook_secret",
                _clean_secret(self.webhook_secret, "GitHub webhook secret"),
            )

        repositories = frozenset(self.allowed_repositories)
        if not repositories:
            raise GitHubConfigurationError("GitHub repository allowlist must not be empty")
        normalized: set[str] = set()
        for repository in repositories:
            if not isinstance(repository, str) or not _REPOSITORY_RE.fullmatch(repository):
                raise GitHubConfigurationError("GitHub repository allowlist contains an invalid name")
            normalized.add(repository.casefold())
        object.__setattr__(self, "allowed_repositories", frozenset(normalized))

        if not isinstance(self.admission_label, str) or not self.admission_label.strip():
            raise GitHubConfigurationError("GitHub admission label must not be blank")
        object.__setattr__(self, "admission_label", self.admission_label.strip())

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> GitHubConfig:
        """Load the allowlist and token from the process environment or a file."""

        values = os.environ if environ is None else environ
        token = values.get("CODEFACTORY_GITHUB_TOKEN")
        if token is None or not token.strip():
            token_file = values.get("CODEFACTORY_GITHUB_TOKEN_FILE")
            if token_file:
                try:
                    token = Path(token_file).expanduser().read_text(encoding="utf-8")
                except OSError:
                    raise GitHubConfigurationError(
                        "GitHub token file could not be read"
                    ) from None
        if token is not None:
            token = _clean_secret(token, "GitHub token")

        raw_repositories = values.get("CODEFACTORY_GITHUB_ALLOWED_REPOSITORIES", "")
        repositories = frozenset(
            item.strip()
            for item in raw_repositories.split(",")
            if item.strip()
        )
        label = values.get("CODEFACTORY_GITHUB_ADMISSION_LABEL", DEFAULT_ADMISSION_LABEL)
        webhook_secret = values.get("CODEFACTORY_GITHUB_WEBHOOK_SECRET")
        return cls(
            token=token,
            allowed_repositories=repositories,
            admission_label=label,
            webhook_secret=webhook_secret,
        )

    def allows_repository(self, repository: str) -> bool:
        return (
            isinstance(repository, str)
            and bool(_REPOSITORY_RE.fullmatch(repository))
            and repository.casefold() in self.allowed_repositories
        )


@dataclass(frozen=True)
class GitHubIssue:
    repository: str
    number: int
    title: str
    body: str | None
    state: str
    labels: tuple[str, ...]
    html_url: str
    updated_at: str | None


@dataclass(frozen=True)
class PullRequest:
    repository: str
    number: int
    title: str
    body: str | None
    state: str
    draft: bool
    merged: bool
    head_sha: str
    head_ref: str
    base_ref: str
    html_url: str
    merged_at: str | None = None


@dataclass(frozen=True)
class CheckRun:
    name: str
    status: str
    conclusion: str | None
    head_sha: str
    completed_at: str | None
    html_url: str | None = None


@dataclass(frozen=True)
class CommitStatus:
    state: str
    contexts: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class CIEvidence:
    repository: str
    pull_request_number: int
    head_sha: str
    combined_status: CommitStatus
    check_runs: tuple[CheckRun, ...]

    @property
    def successful(self) -> bool:
        has_legacy_contexts = bool(self.combined_status.contexts)
        has_evidence = has_legacy_contexts or bool(self.check_runs)
        legacy_status_passes = self.combined_status.state == "success" or (
            self.combined_status.state == "pending" and not has_legacy_contexts
        )
        return (
            has_evidence
            and legacy_status_passes
            and all(
                run.status == "completed" and run.conclusion == "success"
                for run in self.check_runs
            )
        )


@dataclass(frozen=True)
class PullRequestReview:
    reviewer: str | None
    state: str
    commit_id: str | None
    submitted_at: str | None
    body: str | None


@dataclass(frozen=True)
class IssueComment:
    comment_id: int
    author: str | None
    body: str
    created_at: str | None
    updated_at: str | None
    html_url: str | None


@dataclass(frozen=True)
class MergeResult:
    merged: bool
    message: str
    merge_commit_sha: str | None


@dataclass(frozen=True)
class VerifiedWebhook:
    delivery_id: str
    event: str
    payload: Mapping[str, Any]


def verify_webhook(
    *,
    secret: str,
    raw_body: bytes,
    signature: str | None,
    event: str | None,
    delivery_id: str | None,
) -> VerifiedWebhook:
    """Verify and parse a GitHub webhook without trusting decoded body data."""

    secret_bytes = _clean_secret(secret, "GitHub webhook secret").encode("utf-8")
    if not isinstance(raw_body, bytes):
        raise ValueError("webhook body must be bytes")
    if not isinstance(signature, str) or not _SIGNATURE_RE.fullmatch(signature):
        raise ValueError("invalid GitHub webhook signature")
    if not isinstance(event, str) or not _EVENT_RE.fullmatch(event):
        raise ValueError("invalid GitHub webhook event")
    if not isinstance(delivery_id, str):
        raise ValueError("invalid GitHub webhook delivery ID")
    try:
        parsed_id = uuid.UUID(delivery_id)
    except (ValueError, AttributeError):
        raise ValueError("invalid GitHub webhook delivery ID") from None
    if str(parsed_id) != delivery_id.lower():
        raise ValueError("invalid GitHub webhook delivery ID")

    expected = "sha256=" + hmac.new(secret_bytes, raw_body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature.lower(), expected):
        raise ValueError("invalid GitHub webhook signature")
    try:
        payload = json.loads(raw_body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("invalid GitHub webhook payload") from None
    if not isinstance(payload, dict):
        raise ValueError("invalid GitHub webhook payload")
    return VerifiedWebhook(delivery_id=delivery_id.lower(), event=event, payload=payload)


class GitHubClient:
    """A narrow GitHub REST API client with injectable HTTP and token providers."""

    def __init__(
        self,
        config: GitHubConfig,
        *,
        http_client: httpx.Client | None = None,
        token_provider: Callable[[], str] | None = None,
        timeout: float = 15.0,
        draft_publication_gate: Callable[..., PullRequest] | None = None,
    ) -> None:
        if config.token is None and token_provider is None:
            raise GitHubConfigurationError(
                "a GitHub token or token provider is required for API access"
            )
        if timeout <= 0:
            raise GitHubConfigurationError("GitHub API timeout must be positive")
        self.config = config
        self.draft_publication_gate = draft_publication_gate
        self._token_provider = token_provider
        self._owns_client = http_client is None
        if http_client is None:
            self._http = httpx.Client(
                base_url=API_ORIGIN,
                timeout=timeout,
                follow_redirects=False,
            )
        else:
            self._validate_http_client(http_client)
            http_client.follow_redirects = False
            self._http = http_client

    def __enter__(self) -> GitHubClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_client:
            self._http.close()

    def list_admitted_issues(self, repository: str) -> list[GitHubIssue]:
        """List open, labeled issues in an explicitly allowlisted repository.

        GitHub's issues endpoint also returns pull requests. Those entries are
        discarded before admission, and pagination uses numeric page parameters
        instead of following server-supplied URLs with credentials attached.
        """

        self._require_repository(repository)
        issues: list[GitHubIssue] = []
        page = 1
        while True:
            response = self._request(
                "GET",
                self._repo_path(repository, "issues"),
                params={"state": "open", "per_page": 100, "page": page},
            )
            if not isinstance(response, list):
                raise GitHubAPIError("GitHub API returned an invalid issue list")
            for item in response:
                if not isinstance(item, dict) or "pull_request" in item:
                    continue
                parsed = self._parse_issue(repository, item)
                if self.config.admission_label.casefold() in {
                    label.casefold() for label in parsed.labels
                }:
                    issues.append(parsed)
            if len(response) < 100:
                break
            page += 1
        return issues

    def get_issue(self, repository: str, issue_number: int) -> GitHubIssue:
        self._require_repository(repository)
        self._require_number(issue_number)
        response = self._request(
            "GET", self._repo_path(repository, f"issues/{issue_number}")
        )
        if not isinstance(response, dict) or "pull_request" in response:
            raise GitHubAPIError("GitHub API returned an invalid issue")
        return self._parse_issue(repository, response)

    def list_issue_comments(
        self, repository: str, issue_number: int
    ) -> tuple[IssueComment, ...]:
        """Read issue/PR timeline comments, including plan approval replies."""

        self._require_repository(repository)
        self._require_number(issue_number)
        page = 1
        comments: list[IssueComment] = []
        while True:
            response = self._request(
                "GET",
                self._repo_path(repository, f"issues/{issue_number}/comments"),
                params={"per_page": 100, "page": page},
            )
            if not isinstance(response, list):
                raise GitHubAPIError("GitHub API returned invalid issue comments")
            for item in response:
                if not isinstance(item, dict):
                    continue
                comment_id, body = item.get("id"), item.get("body")
                if (
                    isinstance(comment_id, bool)
                    or not isinstance(comment_id, int)
                    or comment_id <= 0
                    or not isinstance(body, str)
                ):
                    raise GitHubAPIError("GitHub API returned invalid issue comments")
                user = item.get("user")
                author = user.get("login") if isinstance(user, dict) else None
                created_at, updated_at, html_url = (
                    item.get("created_at"), item.get("updated_at"), item.get("html_url")
                )
                comments.append(
                    IssueComment(
                        comment_id=comment_id,
                        author=author if isinstance(author, str) else None,
                        body=body,
                        created_at=created_at if isinstance(created_at, str) else None,
                        updated_at=updated_at if isinstance(updated_at, str) else None,
                        html_url=html_url if isinstance(html_url, str) else None,
                    )
                )
            if len(response) < 100:
                break
            page += 1
        return tuple(comments)

    def create_issue_comment(
        self, repository: str, issue_number: int, *, body: str
    ) -> IssueComment:
        """Post a controller-approved comment, such as a plan approval prompt."""

        self._require_repository(repository)
        self._require_number(issue_number)
        self._require_text(body, "issue comment")
        response = self._request(
            "POST",
            self._repo_path(repository, f"issues/{issue_number}/comments"),
            json={"body": body},
        )
        if not isinstance(response, dict):
            raise GitHubAPIError("GitHub API returned invalid issue comment")
        comment_id, comment_body = response.get("id"), response.get("body")
        if (
            isinstance(comment_id, bool)
            or not isinstance(comment_id, int)
            or comment_id <= 0
            or not isinstance(comment_body, str)
        ):
            raise GitHubAPIError("GitHub API returned invalid issue comment")
        user = response.get("user")
        author = user.get("login") if isinstance(user, dict) else None
        created_at, updated_at, html_url = (
            response.get("created_at"), response.get("updated_at"), response.get("html_url")
        )
        return IssueComment(
            comment_id=comment_id,
            author=author if isinstance(author, str) else None,
            body=comment_body,
            created_at=created_at if isinstance(created_at, str) else None,
            updated_at=updated_at if isinstance(updated_at, str) else None,
            html_url=html_url if isinstance(html_url, str) else None,
        )

    def get_pull_request(self, repository: str, pull_number: int) -> PullRequest:
        self._require_repository(repository)
        self._require_number(pull_number)
        response = self._request(
            "GET", self._repo_path(repository, f"pulls/{pull_number}")
        )
        return self._parse_pull_request(repository, response)

    def list_active_pull_requests(self, repository: str) -> list[PullRequest]:
        """Complete bounded inventory: every open draft and ready PR counts."""
        self._require_repository(repository)
        pulls = []
        for page in range(1, 101):
            response = self._request("GET", self._repo_path(repository, "pulls"), params={"state": "open", "per_page": 100, "page": page})
            if not isinstance(response, list):
                raise GitHubAPIError("GitHub returned an invalid active PR inventory")
            for item in response:
                if not isinstance(item, dict):
                    raise GitHubAPIError("GitHub returned an invalid active PR inventory")
                pull = self._parse_pull_request(repository, {**item, "merged": bool(item.get("merged_at"))})
                if pull.state != "open" or pull.merged:
                    raise GitHubAPIError("GitHub returned an inconsistent active PR inventory")
                pulls.append(pull)
            if len(response) < 100:
                return pulls
        raise GitHubAPIError("active PR inventory exceeded the pagination bound")

    def get_check_runs(self, repository: str, head_sha: str) -> tuple[CheckRun, ...]:
        self._require_repository(repository)
        self._require_sha(head_sha)
        page = 1
        runs: list[CheckRun] = []
        while True:
            response = self._request(
                "GET",
                self._repo_path(repository, f"commits/{head_sha}/check-runs"),
                params={"per_page": 100, "page": page},
            )
            if not isinstance(response, dict) or not isinstance(
                response.get("check_runs"), list
            ):
                raise GitHubAPIError("GitHub API returned invalid check run evidence")
            items = response["check_runs"]
            for item in items:
                if not isinstance(item, dict) or item.get("head_sha") != head_sha:
                    continue
                runs.append(self._parse_check_run(item))
            if len(items) < 100:
                break
            page += 1
        return tuple(runs)

    def get_commit_status(self, repository: str, head_sha: str) -> CommitStatus:
        self._require_repository(repository)
        self._require_sha(head_sha)
        response = self._request(
            "GET", self._repo_path(repository, f"commits/{head_sha}/status")
        )
        if not isinstance(response, dict):
            raise GitHubAPIError("GitHub API returned invalid commit status evidence")
        state = response.get("state")
        statuses = response.get("statuses")
        if state not in {"pending", "success", "failure", "error"} or not isinstance(
            statuses, list
        ):
            raise GitHubAPIError("GitHub API returned invalid commit status evidence")
        contexts: list[tuple[str, str]] = []
        for item in statuses:
            if not isinstance(item, dict):
                continue
            context, status = item.get("context"), item.get("state")
            if isinstance(context, str) and isinstance(status, str):
                contexts.append((context, status))
        return CommitStatus(state=state, contexts=tuple(contexts))

    def get_ci_evidence(self, repository: str, pull_number: int) -> CIEvidence:
        """Read CI evidence for the pull request's current head commit."""

        pull = self.get_pull_request(repository, pull_number)
        # Both endpoints are SHA-pinned so evidence from a previous push cannot
        # satisfy a check for the current pull request head.
        status = self.get_commit_status(repository, pull.head_sha)
        runs = self.get_check_runs(repository, pull.head_sha)
        return CIEvidence(
            repository=repository,
            pull_request_number=pull_number,
            head_sha=pull.head_sha,
            combined_status=status,
            check_runs=runs,
        )

    def list_pull_request_reviews(
        self, repository: str, pull_number: int
    ) -> tuple[PullRequestReview, ...]:
        self._require_repository(repository)
        self._require_number(pull_number)
        page = 1
        reviews: list[PullRequestReview] = []
        while True:
            response = self._request(
                "GET",
                self._repo_path(repository, f"pulls/{pull_number}/reviews"),
                params={"per_page": 100, "page": page},
            )
            if not isinstance(response, list):
                raise GitHubAPIError("GitHub API returned invalid review data")
            for item in response:
                if not isinstance(item, dict):
                    continue
                user = item.get("user")
                login = user.get("login") if isinstance(user, dict) else None
                reviews.append(
                    PullRequestReview(
                        reviewer=login if isinstance(login, str) else None,
                        state=item.get("state") if isinstance(item.get("state"), str) else "",
                        commit_id=item.get("commit_id") if isinstance(item.get("commit_id"), str) else None,
                        submitted_at=item.get("submitted_at") if isinstance(item.get("submitted_at"), str) else None,
                        body=item.get("body") if isinstance(item.get("body"), str) else None,
                    )
                )
            if len(response) < 100:
                break
            page += 1
        return tuple(reviews)

    def create_draft_pull_request(
        self,
        repository: str,
        *,
        title: str,
        head: str,
        base: str,
        body: str = "",
        task_id: int | None = None,
    ) -> PullRequest:
        self._require_repository(repository)
        self._require_text(title, "pull request title")
        self._require_text(head, "pull request head")
        self._require_text(base, "pull request base")
        if not isinstance(body, str):
            raise ValueError("pull request body must be text")
        if self.draft_publication_gate is None:
            raise GitHubConfigurationError("draft publication capacity gate is not configured")
        def publish():
            response = self._request("POST", self._repo_path(repository, "pulls"), json={"title": title, "head": head, "base": base, "body": body, "draft": True})
            return self._parse_pull_request(repository, response)
        return self.draft_publication_gate(self, repository, head, base, publish, task_id=task_id)

    def submit_pull_request_review(
        self,
        repository: str,
        pull_number: int,
        *,
        event: str,
        body: str,
        commit_id: str | None = None,
    ) -> PullRequestReview:
        self._require_repository(repository)
        self._require_number(pull_number)
        self._require_text(body, "review body")
        if event not in {"COMMENT", "APPROVE", "REQUEST_CHANGES"}:
            raise ValueError("invalid pull request review event")
        request_body: dict[str, str] = {"event": event, "body": body}
        if commit_id is not None:
            self._require_sha(commit_id)
            request_body["commit_id"] = commit_id
        response = self._request(
            "POST",
            self._repo_path(repository, f"pulls/{pull_number}/reviews"),
            json=request_body,
        )
        if not isinstance(response, dict):
            raise GitHubAPIError("GitHub API returned invalid review data")
        user = response.get("user")
        login = user.get("login") if isinstance(user, dict) else None
        return PullRequestReview(
            reviewer=login if isinstance(login, str) else None,
            state=response.get("state") if isinstance(response.get("state"), str) else "",
            commit_id=response.get("commit_id") if isinstance(response.get("commit_id"), str) else None,
            submitted_at=response.get("submitted_at") if isinstance(response.get("submitted_at"), str) else None,
            body=response.get("body") if isinstance(response.get("body"), str) else None,
        )

    def merge_pull_request(
        self,
        repository: str,
        pull_number: int,
        *,
        expected_head_sha: str,
        merge_method: str = "squash",
        commit_title: str | None = None,
        commit_message: str | None = None,
    ) -> MergeResult:
        """Merge only if GitHub still sees the expected reviewed head SHA.

        Human approval and all workflow gates belong to the controller before
        calling this low-level operation.
        """

        self._require_repository(repository)
        self._require_number(pull_number)
        self._require_sha(expected_head_sha)
        if merge_method not in {"merge", "squash", "rebase"}:
            raise ValueError("invalid pull request merge method")
        request_body: dict[str, str] = {
            "sha": expected_head_sha,
            "merge_method": merge_method,
        }
        if commit_title is not None:
            request_body["commit_title"] = commit_title
        if commit_message is not None:
            request_body["commit_message"] = commit_message
        response = self._request(
            "PUT",
            self._repo_path(repository, f"pulls/{pull_number}/merge"),
            json=request_body,
        )
        if not isinstance(response, dict) or not isinstance(response.get("merged"), bool):
            raise GitHubAPIError("GitHub API returned invalid merge result")
        sha = response.get("sha")
        return MergeResult(
            merged=response["merged"],
            message=response.get("message") if isinstance(response.get("message"), str) else "",
            merge_commit_sha=sha if isinstance(sha, str) else None,
        )

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
            "Authorization": f"Bearer {self._get_token()}",
        }
        try:
            response = self._http.request(
                method,
                f"{API_ORIGIN}{path}",
                headers=headers,
                **kwargs,
            )
        except httpx.HTTPError:
            raise GitHubAPIError("GitHub API request failed") from None
        if not response.is_success:
            raise GitHubAPIError(
                f"GitHub API request failed with HTTP {response.status_code}",
                status_code=response.status_code,
            )
        if response.status_code == 204:
            return None
        try:
            return response.json()
        except (ValueError, UnicodeDecodeError):
            raise GitHubAPIError("GitHub API returned invalid JSON") from None

    def _get_token(self) -> str:
        try:
            token = self._token_provider() if self._token_provider else self.config.token
            if token is None:
                raise ValueError
            return _clean_secret(token, "GitHub token")
        except Exception:
            raise GitHubConfigurationError("GitHub token provider failed") from None

    def _require_repository(self, repository: str) -> None:
        if not isinstance(repository, str) or not self.config.allows_repository(repository):
            raise ValueError("repository is not in the GitHub allowlist")

    @staticmethod
    def _require_number(number: int) -> None:
        if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
            raise ValueError("GitHub issue or pull request number must be positive")

    @staticmethod
    def _require_sha(sha: str) -> None:
        if not isinstance(sha, str) or not _SHA_RE.fullmatch(sha):
            raise ValueError("invalid GitHub commit SHA")

    @staticmethod
    def _require_text(value: str, field_name: str) -> None:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field_name} must not be blank")

    @staticmethod
    def _repo_path(repository: str, resource: str) -> str:
        owner, name = repository.split("/", 1)
        return f"/repos/{owner}/{name}/{resource}"

    @classmethod
    def _parse_issue(cls, repository: str, item: Mapping[str, Any]) -> GitHubIssue:
        number, title, state, html_url = (
            item.get("number"), item.get("title"), item.get("state"), item.get("html_url")
        )
        labels_value = item.get("labels", [])
        if (
            isinstance(number, bool)
            or not isinstance(number, int)
            or number <= 0
            or not isinstance(title, str)
            or not isinstance(state, str)
            or not isinstance(html_url, str)
            or not isinstance(labels_value, list)
        ):
            raise GitHubAPIError("GitHub API returned an invalid issue")
        labels: list[str] = []
        for label in labels_value:
            if isinstance(label, dict) and isinstance(label.get("name"), str):
                labels.append(label["name"])
            elif isinstance(label, str):
                labels.append(label)
        body = item.get("body")
        updated_at = item.get("updated_at")
        return GitHubIssue(
            repository=repository,
            number=number,
            title=title,
            body=body if isinstance(body, str) else None,
            state=state,
            labels=tuple(labels),
            html_url=html_url,
            updated_at=updated_at if isinstance(updated_at, str) else None,
        )

    @classmethod
    def _parse_pull_request(cls, repository: str, item: Any) -> PullRequest:
        if not isinstance(item, dict):
            raise GitHubAPIError("GitHub API returned an invalid pull request")
        number = item.get("number")
        title, state, draft, merged = (
            item.get("title"), item.get("state"), item.get("draft"), item.get("merged")
        )
        head, base = item.get("head"), item.get("base")
        html_url = item.get("html_url")
        head_sha = head.get("sha") if isinstance(head, dict) else None
        head_ref = head.get("ref") if isinstance(head, dict) else None
        base_ref = base.get("ref") if isinstance(base, dict) else None
        if (
            isinstance(number, bool)
            or not isinstance(number, int)
            or number <= 0
            or not isinstance(title, str)
            or not isinstance(state, str)
            or not isinstance(draft, bool)
            or not isinstance(merged, bool)
            or not isinstance(head_sha, str)
            or not _SHA_RE.fullmatch(head_sha)
            or not isinstance(head_ref, str)
            or not isinstance(base_ref, str)
            or not isinstance(html_url, str)
        ):
            raise GitHubAPIError("GitHub API returned an invalid pull request")
        body, merged_at = item.get("body"), item.get("merged_at")
        return PullRequest(
            repository=repository,
            number=number,
            title=title,
            body=body if isinstance(body, str) else None,
            state=state,
            draft=draft,
            merged=merged,
            head_sha=head_sha,
            head_ref=head_ref,
            base_ref=base_ref,
            html_url=html_url,
            merged_at=merged_at if isinstance(merged_at, str) else None,
        )

    @classmethod
    def _parse_check_run(cls, item: Mapping[str, Any]) -> CheckRun:
        name, status, conclusion, head_sha = (
            item.get("name"), item.get("status"), item.get("conclusion"), item.get("head_sha")
        )
        if (
            not isinstance(name, str)
            or not isinstance(status, str)
            or (conclusion is not None and not isinstance(conclusion, str))
            or not isinstance(head_sha, str)
            or not _SHA_RE.fullmatch(head_sha)
        ):
            raise GitHubAPIError("GitHub API returned invalid check run evidence")
        completed_at, html_url = item.get("completed_at"), item.get("html_url")
        return CheckRun(
            name=name,
            status=status,
            conclusion=conclusion,
            head_sha=head_sha,
            completed_at=completed_at if isinstance(completed_at, str) else None,
            html_url=html_url if isinstance(html_url, str) else None,
        )

    @staticmethod
    def _validate_http_client(client: httpx.Client) -> None:
        base_url = client.base_url
        if (
            base_url.scheme != "https"
            or base_url.host != "api.github.com"
            or base_url.port not in {None, 443}
            or base_url.path not in {"", "/"}
            or base_url.query
            or base_url.fragment
            or base_url.username not in {None, ""}
            or base_url.password not in {None, ""}
            or client.auth is not None
        ):
            raise GitHubConfigurationError(
                "injected HTTP client must target the GitHub API origin without custom auth"
            )


def _clean_secret(value: str, name: str) -> str:
    if not isinstance(value, str):
        raise GitHubConfigurationError(f"{name} must be text")
    clean = value.strip()
    if not clean or any(character.isspace() for character in clean):
        raise GitHubConfigurationError(f"{name} is missing or invalid")
    return clean
