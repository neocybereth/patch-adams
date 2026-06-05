from fastapi.testclient import TestClient

import app.main as main


def test_dashboard_renders_empty_run_list(monkeypatch) -> None:
    monkeypatch.setattr(main, "get_runs", lambda: [])
    monkeypatch.setattr(main, "get_events_for_runs", lambda run_ids: {})

    client = TestClient(main.app)
    response = client.get("/")

    assert response.status_code == 200
    assert "Total runs" in response.text
    assert ">8<" in response.text
    assert "SQL Lab: query history pagination resets on tab switch" in response.text


def test_markdown_renders_plain_code_fragments_as_inline_code() -> None:
    rendered = main.render_markdown(
        "Adding role=\"status\" to the <EmptyContainer> element in "
        "EmptyState.tsx requires screen.getByRole('status')."
    )

    assert '<code>role="status"</code>' in rendered
    assert "<code>&lt;EmptyContainer&gt;</code>" in rendered
    assert "<code>EmptyState.tsx</code>" in rendered
    assert "<code>screen.getByRole('status')</code>" in rendered


def test_markdown_keeps_untrusted_html_escaped() -> None:
    rendered = main.render_markdown("Render <script>alert('xss')</script> safely.")

    assert "<script>" not in rendered
    assert "<code>&lt;script&gt;</code>" in rendered
    assert "<code>&lt;/script&gt;</code>" in rendered


def test_dashboard_renders_issue_card_timeline_and_events(monkeypatch) -> None:
    monkeypatch.setattr(
        main,
        "get_runs",
        lambda: [
            {
                "id": "run-1",
                "issue_number": 42,
                "issue_url": "https://github.com/apache/superset/issues/42",
                "issue_title": "Fix chart bug",
                "status": "pr_created",
                "triage_decision": "autofix",
                "triage_risk": "low",
                "pr_url": "https://github.com/acme/superset/pull/42",
                "tests_run": ["pytest tests/test_chart.py"],
            }
        ],
    )
    monkeypatch.setattr(
        main,
        "get_events_for_runs",
        lambda run_ids: {
            "run-1": [
                {
                    "event_type": "received",
                    "message": "Received issue #42",
                    "created_at": "2026-06-05T00:00:00Z",
                },
                {
                    "event_type": "pr_created",
                    "message": "PR created",
                    "created_at": "2026-06-05T00:05:00Z",
                },
            ],
        },
    )

    client = TestClient(main.app)
    response = client.get("/")

    assert response.status_code == 200
    assert "Fix chart bug" in response.text
    assert "Pull Request" in response.text
    assert "Run events" in response.text
    assert "PR created" in response.text


def test_failed_run_marks_current_timeline_step_as_failed() -> None:
    timeline = main.build_timeline({"status": "failed"})

    assert timeline[0]["state"] == "completed"
    assert timeline[1]["state"] == "completed"
    assert timeline[2]["state"] == "failed"
    assert timeline[3]["state"] == "pending"


def test_dashboard_shows_retry_for_transient_devin_timeout(monkeypatch) -> None:
    monkeypatch.setattr(
        main,
        "get_runs",
        lambda: [
            {
                "id": "run-1",
                "issue_number": 42,
                "issue_title": "Fix chart bug",
                "status": "failed",
                "triage_session_id": "devin-1",
                "remediation_session_id": "devin-1",
                "error": "HTTPSConnectionPool(host='api.devin.ai', port=443): Read timed out. (read timeout=30)",
            }
        ],
    )
    monkeypatch.setattr(main, "get_events_for_runs", lambda run_ids: {})

    client = TestClient(main.app)
    response = client.get("/")

    assert response.status_code == 200
    assert "Retry Devin call" in response.text
    assert "/runs/run-1/retry" in response.text


def test_dashboard_hides_retry_for_non_timeout_failure(monkeypatch) -> None:
    monkeypatch.setattr(
        main,
        "get_runs",
        lambda: [
            {
                "id": "run-1",
                "issue_number": 42,
                "issue_title": "Fix chart bug",
                "status": "failed",
                "error": "Could not parse PR or blocker details from terminal remediation output.",
            }
        ],
    )
    monkeypatch.setattr(main, "get_events_for_runs", lambda run_ids: {})

    client = TestClient(main.app)
    response = client.get("/")

    assert response.status_code == 200
    assert "Retry Devin call" not in response.text
    assert "/runs/run-1/retry" not in response.text


def test_dashboard_shows_waiting_action(monkeypatch) -> None:
    monkeypatch.setattr(
        main,
        "get_runs",
        lambda: [
            {
                "id": "run-1",
                "issue_number": 42,
                "issue_title": "Fix chart bug",
                "status": "needs_human",
                "triage_session_id": "devin-1",
                "triage_session_url": "https://app.devin.ai/sessions/devin-1",
                "triage_recommended_fix": "Reply to Devin with approval.",
            }
        ],
    )
    monkeypatch.setattr(main, "get_events_for_runs", lambda run_ids: {})

    client = TestClient(main.app)
    response = client.get("/")

    assert response.status_code == 200
    assert "Devin is waiting for instructions." in response.text
    assert "Reply to Devin with approval." in response.text
    assert "/runs/run-1/continue" in response.text


def test_dashboard_shows_hard_handoff_without_continue_action(monkeypatch) -> None:
    monkeypatch.setattr(
        main,
        "get_runs",
        lambda: [
            {
                "id": "run-1",
                "issue_number": 42,
                "issue_title": "Hard issue",
                "status": "needs_human",
                "triage_risk": "hard",
                "triage_session_id": "devin-1",
                "triage_session_url": "https://app.devin.ai/sessions/devin-1",
                "triage_recommended_fix": "Start this issue in Devin: https://app.devin.ai/sessions/devin-1",
            }
        ],
    )
    monkeypatch.setattr(main, "get_events_for_runs", lambda run_ids: {})

    client = TestClient(main.app)
    response = client.get("/")

    assert response.status_code == 200
    assert "Human-in-the-loop Devin launch recommended." in response.text
    assert "Start this issue in Devin" in response.text
    assert "https://app.devin.ai/sessions/devin-1" in response.text
    assert "/runs/run-1/continue" not in response.text


def test_continue_run_reactivates_waiting_remediation(monkeypatch) -> None:
    updates: list[main.JsonObject] = []
    events: list[main.JsonObject] = []

    monkeypatch.setattr(
        main,
        "get_run",
        lambda run_id: {
            "id": run_id,
            "status": "needs_human",
            "remediation_session_id": "devin-2",
        },
    )

    def fake_update_run(run_id: str, fields: main.JsonObject) -> main.JsonObject:
        updates.append({"run_id": run_id, **fields})
        return {"id": run_id, **fields}

    def fake_log_event(
        run_id: str,
        event_type: str,
        message: str | None = None,
        metadata: main.JsonObject | None = None,
    ) -> None:
        events.append({
            "run_id": run_id,
            "event_type": event_type,
            "message": message,
            "metadata": metadata or {},
        })

    monkeypatch.setattr(main, "update_run", fake_update_run)
    monkeypatch.setattr(main, "log_event", fake_log_event)

    client = TestClient(main.app)
    response = client.post("/runs/run-1/continue", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/#run-run-1"
    assert updates == [
        {
            "run_id": "run-1",
            "status": "remediation_started",
            "error": None,
            "completed_at": None,
        }
    ]
    assert events[0]["event_type"] == "human_continued"


def test_retry_run_reactivates_transient_remediation_timeout(monkeypatch) -> None:
    updates: list[main.JsonObject] = []
    events: list[main.JsonObject] = []

    monkeypatch.setattr(
        main,
        "get_run",
        lambda run_id: {
            "id": run_id,
            "status": "failed",
            "remediation_session_id": "devin-2",
            "error": "HTTPSConnectionPool(host='api.devin.ai', port=443): Read timed out. (read timeout=30)",
        },
    )

    def fake_update_run(run_id: str, fields: main.JsonObject) -> main.JsonObject:
        updates.append({"run_id": run_id, **fields})
        return {"id": run_id, **fields}

    def fake_log_event(
        run_id: str,
        event_type: str,
        message: str | None = None,
        metadata: main.JsonObject | None = None,
    ) -> None:
        events.append({
            "run_id": run_id,
            "event_type": event_type,
            "message": message,
            "metadata": metadata or {},
        })

    monkeypatch.setattr(main, "update_run", fake_update_run)
    monkeypatch.setattr(main, "log_event", fake_log_event)

    client = TestClient(main.app)
    response = client.post("/runs/run-1/retry", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/#run-run-1"
    assert updates == [
        {
            "run_id": "run-1",
            "status": "remediation_started",
            "error": None,
            "completed_at": None,
        }
    ]
    assert events[0]["event_type"] == "transient_failure_retried"
    assert events[0]["metadata"] == {"next_status": "remediation_started"}


def test_worker_tick_auto_requests_review_after_pr_created(monkeypatch) -> None:
    monkeypatch.setattr(
        main,
        "get_active_runs",
        lambda: [{"id": "run-1", "status": "remediation_started"}],
    )
    monkeypatch.setattr(
        main,
        "advance_run_through_local_steps",
        lambda run: [
            {"run_id": run["id"], "action": "pr_created"},
            {"run_id": run["id"], "action": "review_started"},
        ],
    )

    client = TestClient(main.app)
    response = client.post("/worker/tick")

    assert response.status_code == 200
    assert response.json()["advanced"] == [
        {"run_id": "run-1", "action": "pr_created"},
        {"run_id": "run-1", "action": "review_started"},
    ]
