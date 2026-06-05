from fastapi.testclient import TestClient

import app.main as main
from app.github_client import GitHubAPIError
from app.runs import JsonObject


def webhook_payload() -> JsonObject:
    return {
        "action": "labeled",
        "label": {"name": "devin"},
        "issue": {
            "number": 42,
            "html_url": "https://github.com/apache/superset/issues/42",
            "title": "Fix chart bug",
        },
        "repository": {"full_name": "apache/superset"},
        "sender": {"login": "octocat"},
    }


def test_github_webhook_posts_acknowledgement_comment(monkeypatch) -> None:
    posted_comments: list[JsonObject] = []

    def fake_create_or_reuse_issue_run(
        issue_number: int,
        issue_url: str,
        issue_title: str,
        metadata: JsonObject | None = None,
    ) -> tuple[JsonObject, bool]:
        return {
            "id": "run-42",
            "issue_number": issue_number,
            "issue_url": issue_url,
            "issue_title": issue_title,
            "status": "received",
        }, True

    def fake_post_issue_comment(
        repository: str,
        issue_number: int,
        body: str,
    ) -> JsonObject:
        posted_comments.append({
            "repository": repository,
            "issue_number": issue_number,
            "body": body,
        })
        return {"html_url": "https://github.com/apache/superset/issues/42#issuecomment-1"}

    monkeypatch.setattr(main, "create_or_reuse_issue_run", fake_create_or_reuse_issue_run)
    monkeypatch.setattr(main, "advance_run", lambda run: {"action": "start_triage"})
    monkeypatch.setattr(main, "get_run", lambda run_id: None)
    monkeypatch.setattr(main, "post_issue_comment", fake_post_issue_comment)
    monkeypatch.setenv("PATCHOPS_PUBLIC_URL", "https://patchops.example.com")

    client = TestClient(main.app)
    response = client.post("/github/webhook", json=webhook_payload())

    assert response.status_code == 200
    assert response.json()["acknowledgement"]["posted"] is True
    assert posted_comments[0]["repository"] == "apache/superset"
    assert posted_comments[0]["issue_number"] == 42
    assert "Patch Adams has picked up this issue" in str(posted_comments[0]["body"])
    assert "https://patchops.example.com/#run-run-42" in str(posted_comments[0]["body"])


def test_github_webhook_keeps_run_when_acknowledgement_fails(monkeypatch) -> None:
    def fake_create_or_reuse_issue_run(
        issue_number: int,
        issue_url: str,
        issue_title: str,
        metadata: JsonObject | None = None,
    ) -> tuple[JsonObject, bool]:
        return {
            "id": "run-42",
            "issue_number": issue_number,
            "issue_url": issue_url,
            "issue_title": issue_title,
            "status": "received",
        }, True

    def fake_post_issue_comment(
        repository: str,
        issue_number: int,
        body: str,
    ) -> JsonObject:
        raise GitHubAPIError("Missing GITHUB_TOKEN")

    monkeypatch.setattr(main, "create_or_reuse_issue_run", fake_create_or_reuse_issue_run)
    monkeypatch.setattr(main, "advance_run", lambda run: {"action": "start_triage"})
    monkeypatch.setattr(main, "get_run", lambda run_id: None)
    monkeypatch.setattr(main, "post_issue_comment", fake_post_issue_comment)

    client = TestClient(main.app)
    response = client.post("/github/webhook", json=webhook_payload())

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["created"] is True
    assert body["acknowledgement"] == {
        "posted": False,
        "reason": "Missing GITHUB_TOKEN",
    }
