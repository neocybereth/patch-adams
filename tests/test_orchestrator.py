import app.orchestrator as orchestrator
from app.runs import JsonObject


def install_transition_capture(
    monkeypatch,
    transitions: list[JsonObject],
) -> None:
    def fake_transition_run(
        run_id: str,
        expected_status: str,
        fields: JsonObject,
        event_type: str,
        message: str,
        metadata: JsonObject | None = None,
    ) -> JsonObject:
        transition = {
            "run_id": run_id,
            "expected_status": expected_status,
            "fields": fields,
            "event_type": event_type,
            "message": message,
            "metadata": metadata or {},
        }
        transitions.append(transition)
        return {"id": run_id, **fields}

    monkeypatch.setattr(orchestrator, "transition_run", fake_transition_run)


def test_medium_risk_autofix_completes_triage(monkeypatch) -> None:
    transitions: list[JsonObject] = []
    install_transition_capture(monkeypatch, transitions)

    def fake_snapshot(session_id: str) -> JsonObject:
        return {
            "status": "running",
            "structured_output": {
                "decision": "autofix",
                "risk": "medium",
                "category": "backend",
                "ui_change_expected": False,
                "reasoning_summary": "Small backend fix.",
                "recommended_fix": "Patch the parser.",
            },
        }

    def fake_messages(session_id: str) -> JsonObject:
        return {"messages": []}

    monkeypatch.setattr(orchestrator, "get_session_snapshot", fake_snapshot)
    monkeypatch.setattr(orchestrator, "get_session_messages", fake_messages)

    result = orchestrator.poll_triage({
        "id": "run-1",
        "status": "triage_started",
        "triage_session_id": "devin-1",
    })

    assert result["next_status"] == "triage_completed"
    assert transitions[0]["fields"]["status"] == "triage_completed"
    assert transitions[0]["fields"]["triage_risk"] == "medium"


def test_triage_json_extracts_from_fenced_message_text(monkeypatch) -> None:
    transitions: list[JsonObject] = []
    install_transition_capture(monkeypatch, transitions)

    def fake_snapshot(session_id: str) -> JsonObject:
        return {
            "status": "running",
            "status_detail": "waiting_for_user",
        }

    def fake_messages(session_id: str) -> JsonObject:
        return {
            "messages": [
                {
                    "body": (
                        "Here is my triage verdict:\n\n"
                        "```json\n"
                        "{\n"
                        '  "decision": "autofix",\n'
                        '  "risk": "low",\n'
                        '  "category": "frontend",\n'
                        '  "ui_change_expected": false,\n'
                        '  "reasoning_summary": "Small semantic fix.",\n'
                        '  "recommended_fix": "Add role status."\n'
                        "}\n"
                        "```"
                    ),
                },
            ],
        }

    monkeypatch.setattr(orchestrator, "get_session_snapshot", fake_snapshot)
    monkeypatch.setattr(orchestrator, "get_session_messages", fake_messages)

    result = orchestrator.poll_triage({
        "id": "run-1",
        "status": "triage_started",
        "triage_session_id": "devin-1",
    })

    assert result["next_status"] == "triage_completed"
    assert transitions[0]["fields"]["status"] == "triage_completed"
    assert transitions[0]["fields"]["triage_decision"] == "autofix"
    assert transitions[0]["fields"]["triage_category"] == "frontend"


def test_hard_triage_posts_human_handoff_comment(monkeypatch) -> None:
    transitions: list[JsonObject] = []
    posted_comments: list[JsonObject] = []
    events: list[JsonObject] = []
    install_transition_capture(monkeypatch, transitions)

    def fake_snapshot(session_id: str) -> JsonObject:
        return {
            "status": "running",
            "structured_output": {
                "decision": "autofix",
                "risk": "hard",
                "category": "backend",
                "ui_change_expected": False,
                "reasoning_summary": "This spans multiple subsystems.",
                "recommended_fix": "Have a human approve scope first.",
            },
        }

    def fake_messages(session_id: str) -> JsonObject:
        return {"messages": []}

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

    def fake_log_event(
        run_id: str,
        event_type: str,
        message: str | None = None,
        metadata: JsonObject | None = None,
    ) -> None:
        events.append({
            "run_id": run_id,
            "event_type": event_type,
            "message": message,
            "metadata": metadata or {},
        })

    monkeypatch.setattr(orchestrator, "get_session_snapshot", fake_snapshot)
    monkeypatch.setattr(orchestrator, "get_session_messages", fake_messages)
    monkeypatch.setattr(orchestrator, "post_issue_comment", fake_post_issue_comment)
    monkeypatch.setattr(orchestrator, "log_event", fake_log_event)

    result = orchestrator.poll_triage({
        "id": "run-1",
        "status": "triage_started",
        "issue_number": 42,
        "issue_url": "https://github.com/apache/superset/issues/42",
        "issue_title": "Hard Superset issue",
        "triage_session_id": "devin-1",
        "triage_session_url": "https://app.devin.ai/sessions/devin-1",
    })

    assert result["action"] == "hard_issue_needs_human"
    assert result["next_status"] == "needs_human"
    assert transitions[0]["event_type"] == "hard_issue_needs_human"
    assert transitions[0]["fields"]["status"] == "needs_human"
    assert transitions[0]["fields"]["triage_risk"] == "hard"
    assert transitions[0]["metadata"]["devin_start_url"] == "https://app.devin.ai/sessions/devin-1"
    assert posted_comments[0]["repository"] == "apache/superset"
    assert posted_comments[0]["issue_number"] == 42
    assert "This could become a large run" in str(posted_comments[0]["body"])
    assert "https://app.devin.ai/sessions/devin-1" in str(posted_comments[0]["body"])
    assert events[0]["event_type"] == "hard_issue_comment_posted"


def test_start_remediation_continues_triage_session(monkeypatch) -> None:
    transitions: list[JsonObject] = []
    sent_messages: list[JsonObject] = []
    install_transition_capture(monkeypatch, transitions)

    def fake_send_session_message(session_id: str, message: str) -> JsonObject:
        sent_messages.append({"session_id": session_id, "message": message})
        return {"session_id": session_id, "status": "running"}

    monkeypatch.setattr(orchestrator, "send_session_message", fake_send_session_message)

    result = orchestrator.start_remediation({
        "id": "run-1",
        "status": "triage_completed",
        "issue_number": 42,
        "triage_session_id": "devin-1",
        "triage_session_url": "https://app.devin.ai/sessions/devin-1",
    })

    assert result["action"] == "start_remediation"
    assert result["session_id"] == "devin-1"
    assert sent_messages == [
        {
            "session_id": "devin-1",
            "message": "proceed with the fix",
        }
    ]
    assert transitions[0]["fields"]["remediation_session_id"] == "devin-1"
    assert transitions[0]["fields"]["remediation_session_url"] == "https://app.devin.ai/sessions/devin-1"
    assert transitions[0]["metadata"]["continued_triage_session"] is True


def test_remediation_pull_request_metadata_advances_to_pr_created(monkeypatch) -> None:
    transitions: list[JsonObject] = []
    install_transition_capture(monkeypatch, transitions)

    def fake_snapshot(session_id: str) -> JsonObject:
        return {
            "status": "running",
            "status_detail": "working",
            "pull_requests": [
                {"pr_url": "https://github.com/acme/superset/pull/42"},
            ],
        }

    def fake_messages(session_id: str) -> JsonObject:
        return {
            "messages": [
                {"body": "Recording: https://cdn.example.com/recording.webm"},
            ],
        }

    monkeypatch.setattr(orchestrator, "get_session_snapshot", fake_snapshot)
    monkeypatch.setattr(orchestrator, "get_session_messages", fake_messages)

    result = orchestrator.poll_remediation({
        "id": "run-1",
        "status": "remediation_started",
        "remediation_session_id": "devin-2",
        "ui_change": True,
    })

    assert result["next_status"] == "pr_created"
    assert transitions[0]["fields"]["pr_url"] == "https://github.com/acme/superset/pull/42"
    assert transitions[0]["fields"]["video_url"] == "https://cdn.example.com/recording.webm"


def test_waiting_remediation_stays_active(monkeypatch) -> None:
    transitions: list[JsonObject] = []
    install_transition_capture(monkeypatch, transitions)

    def fake_snapshot(session_id: str) -> JsonObject:
        return {"status": "running", "status_detail": "waiting_for_approval"}

    def fake_messages(session_id: str) -> JsonObject:
        return {"messages": []}

    monkeypatch.setattr(orchestrator, "get_session_snapshot", fake_snapshot)
    monkeypatch.setattr(orchestrator, "get_session_messages", fake_messages)

    result = orchestrator.poll_remediation({
        "id": "run-1",
        "status": "remediation_started",
        "remediation_session_id": "devin-2",
    })

    assert result["action"] == "remediation_waiting"
    assert result["status_detail"] == "waiting_for_approval"
    assert transitions == []


def test_review_completion_then_report_ready(monkeypatch) -> None:
    transitions: list[JsonObject] = []
    archived_sessions: list[str] = []
    install_transition_capture(monkeypatch, transitions)

    def fake_get_pr_review(pr_url: str, commit_sha: str | None = None) -> JsonObject:
        return {"status": "completed", "summary": "No blocking review findings."}

    def fake_archive_session(session_id: str) -> JsonObject:
        archived_sessions.append(session_id)
        return {"session_id": session_id, "is_archived": True}

    monkeypatch.setattr(orchestrator, "get_pr_review", fake_get_pr_review)
    monkeypatch.setattr(orchestrator, "archive_session", fake_archive_session)

    review_result = orchestrator.poll_review({
        "id": "run-1",
        "status": "review_started",
        "pr_url": "https://github.com/acme/superset/pull/42",
        "review_session_id": "abc123def456",
        "remediation_session_id": "devin-2",
    })
    report_result = orchestrator.complete_report({
        "id": "run-1",
        "status": "review_completed",
        "triage_decision": "autofix",
        "triage_risk": "low",
        "pr_url": "https://github.com/acme/superset/pull/42",
        "review_status": "completed",
        "tests_run": ["pytest tests/test_parser.py"],
    })

    assert review_result["action"] == "review_completed"
    assert report_result["action"] == "report_ready"
    assert transitions[0]["fields"]["status"] == "review_completed"
    assert transitions[0]["metadata"]["archived_session"] == {
        "session_id": "devin-2",
        "is_archived": True,
    }
    assert transitions[1]["fields"]["status"] == "report_ready"
    assert archived_sessions == ["devin-2"]


def test_auto_advance_requests_review_after_pr_created(monkeypatch) -> None:
    status_by_run = {"run-1": "remediation_started"}

    def fake_advance_run(run: JsonObject) -> JsonObject:
        current_status = str(run["status"])
        if current_status == "remediation_started":
            status_by_run[str(run["id"])] = "pr_created"
            return {"run_id": run["id"], "action": "pr_created"}
        if current_status == "pr_created":
            status_by_run[str(run["id"])] = "review_started"
            return {"run_id": run["id"], "action": "review_started"}
        return {"run_id": run["id"], "action": "no_op"}

    def fake_get_run(run_id: str) -> JsonObject:
        return {
            "id": run_id,
            "status": status_by_run[run_id],
            "pr_url": "https://github.com/acme/superset/pull/42",
        }

    monkeypatch.setattr(orchestrator, "advance_run", fake_advance_run)
    monkeypatch.setattr(orchestrator, "get_run", fake_get_run)

    result = orchestrator.advance_run_through_local_steps({
        "id": "run-1",
        "status": "remediation_started",
    })

    assert result == [
        {"run_id": "run-1", "action": "pr_created"},
        {"run_id": "run-1", "action": "review_started"},
    ]


def test_auto_advance_starts_remediation_after_triage_completed(monkeypatch) -> None:
    status_by_run = {"run-1": "triage_completed"}

    def fake_advance_run(run: JsonObject) -> JsonObject:
        current_status = str(run["status"])
        if current_status == "triage_completed":
            status_by_run[str(run["id"])] = "remediation_started"
            return {"run_id": run["id"], "action": "start_remediation"}
        return {"run_id": run["id"], "action": "no_op"}

    def fake_get_run(run_id: str) -> JsonObject:
        return {
            "id": run_id,
            "status": status_by_run[run_id],
            "remediation_session_id": "devin-1",
        }

    monkeypatch.setattr(orchestrator, "advance_run", fake_advance_run)
    monkeypatch.setattr(orchestrator, "get_run", fake_get_run)

    result = orchestrator.advance_run_through_local_steps({
        "id": "run-1",
        "status": "triage_completed",
    })

    assert result == [
        {"run_id": "run-1", "action": "start_remediation"},
    ]


def test_start_review_tracks_pr_review_commit_sha(monkeypatch) -> None:
    transitions: list[JsonObject] = []
    install_transition_capture(monkeypatch, transitions)

    def fake_request_devin_review(pr_url: str) -> JsonObject:
        return {
            "status": "pending",
            "repo_path": "github.com/acme/superset",
            "pr_number": 42,
            "commit_sha": "abc123def456",
            "created_at": "2026-06-05T08:00:00Z",
        }

    monkeypatch.setattr(orchestrator, "request_devin_review", fake_request_devin_review)

    result = orchestrator.start_review({
        "id": "run-1",
        "status": "pr_created",
        "pr_url": "https://github.com/acme/superset/pull/42",
    })

    assert result["action"] == "review_started"
    assert result["review_id"] == "abc123def456"
    assert transitions[0]["fields"]["review_session_id"] == "abc123def456"
    assert transitions[0]["fields"]["review_status"] == "requested"


def test_start_review_fails_when_review_api_fails(monkeypatch) -> None:
    failures: list[JsonObject] = []
    sent_messages: list[JsonObject] = []

    def fake_request_devin_review(pr_url: str) -> JsonObject:
        raise RuntimeError("Devin review request failed 404: Not Found")

    def fake_send_session_message(session_id: str, message: str) -> JsonObject:
        sent_messages.append({"session_id": session_id, "message": message})
        return {"session_id": session_id}

    def fake_fail_run(run_id: str, error: str) -> None:
        failures.append({"run_id": run_id, "error": error})

    monkeypatch.setattr(orchestrator, "request_devin_review", fake_request_devin_review)
    monkeypatch.setattr(orchestrator, "send_session_message", fake_send_session_message)
    monkeypatch.setattr(orchestrator, "fail_run", fake_fail_run)

    result = orchestrator.start_review({
        "id": "run-1",
        "status": "pr_created",
        "pr_url": "https://github.com/acme/superset/pull/42",
        "remediation_session_id": "devin-2",
    })

    assert result["action"] == "failed"
    assert failures == [
        {
            "run_id": "run-1",
            "error": "Devin review request failed 404: Not Found",
        }
    ]
    assert sent_messages == []
