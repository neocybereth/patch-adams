import json
import re
from typing import TypeAlias, cast
from urllib.parse import urlparse

from app.devin_client import (
    archive_session,
    create_session,
    get_pr_review,
    get_session_snapshot,
    get_session_messages,
    request_devin_review,
    send_session_message,
)
from app.github_client import GitHubAPIError, post_issue_comment
from app.prompts import triage_prompt, remediation_followup_prompt
from app.runs import (
    JsonObject,
    JsonValue,
    fail_run,
    get_run,
    log_event,
    transition_run,
    utc_now,
)

TransitionResult: TypeAlias = dict[str, JsonValue]

WAITING_DETAILS = {"waiting_for_user", "waiting_for_approval"}
ACTIVE_DEVIN_STATUSES = {"running", "starting", "queued"}
TERMINAL_DEVIN_STATUSES = {"exit", "error", "suspended"}
AUTO_ADVANCE_STATUSES = {"triage_completed", "pr_created", "review_completed"}
MAX_AUTO_ADVANCE_STEPS = 3
DEVIN_APP_URL = "https://app.devin.ai"


def advance_run(run: JsonObject) -> TransitionResult:
    """
    Advances one run by one state transition.
    """

    status = str(run["status"])

    if status == "received":
        return start_triage(run)

    if status == "triage_started":
        return poll_triage(run)

    if status == "triage_completed":
        return start_remediation(run)

    if status == "remediation_started":
        return poll_remediation(run)

    if status == "pr_created":
        return start_review(run)

    if status == "review_started":
        return poll_review(run)

    if status == "review_completed":
        return complete_report(run)

    return {
        "run_id": str(run["id"]),
        "status": status,
        "action": "no_op",
    }


def advance_run_through_local_steps(run: JsonObject) -> list[TransitionResult]:
    advanced: list[TransitionResult] = []
    current_run = run

    for _ in range(MAX_AUTO_ADVANCE_STEPS):
        result = advance_run(current_run)
        advanced.append(result)

        latest_run = get_run(str(current_run["id"]))
        if not latest_run or as_optional_str(latest_run.get("status")) not in AUTO_ADVANCE_STATUSES:
            break

        current_run = latest_run

    return advanced


def start_triage(run: JsonObject) -> TransitionResult:
    """
    Starts a Devin triage session.
    Devin should investigate the issue but not modify code.
    """

    try:
        prompt = triage_prompt(
            issue_title=run["issue_title"],
            issue_url=run["issue_url"],
        )

        session = create_session(
            prompt=prompt,
            tags=[
                "takehome",
                "superset",
                "triage",
                f"issue-{run['issue_number']}",
            ],
        )

        session_id = str(session["session_id"])
        session_url = str(session["url"])

        updated = transition_run(
            run_id=str(run["id"]),
            expected_status="received",
            fields={
            "status": "triage_started",
            "triage_session_id": session_id,
            "triage_session_url": session_url,
            "error": None,
            },
            event_type="triage_started",
            message=f"Started Devin triage session for issue #{run['issue_number']}",
            metadata={
                "session_id": session_id,
                "session_url": session_url,
            },
        )

        if not updated:
            return stale_transition(run, "start_triage")

        return {
            "run_id": str(run["id"]),
            "action": "start_triage",
            "session_id": session_id,
            "session_url": session_url,
        }

    except Exception as e:
        fail_run(str(run["id"]), str(e))
        return {
            "run_id": str(run["id"]),
            "action": "failed",
            "error": str(e),
        }


def poll_triage(run: JsonObject) -> TransitionResult:
    """
    Polls Devin triage.

    Important behavior:
    Devin may show a completed answer in messages while session status is still
    "running", so we try parsing messages even while running.
    """

    try:
        session_id = as_optional_str(run.get("triage_session_id"))

        if not session_id:
            raise ValueError("Missing triage_session_id")

        session = get_session_snapshot(session_id)
        devin_status = as_optional_str(session.get("status"))
        status_detail = normalize_session_detail(session.get("status_detail"))
        messages = get_session_messages(session_id)
        payload = {
            "session": session,
            "messages": messages,
        }
        triage = maybe_extract_triage_json(payload)

        if triage:
            return complete_triage(
                run=run,
                triage=triage,
                raw_output={"session": session, "messages": messages},
                devin_status=devin_status,
            )

        if status_detail in WAITING_DETAILS:
            return complete_triage(
                run=run,
                triage={
                    "decision": "needs_human",
                    "risk": "medium",
                    "category": "unknown",
                    "ui_change_expected": False,
                    "reasoning_summary": f"Devin triage is waiting: {status_detail}.",
                    "recommended_fix": extract_waiting_instruction(payload)
                    or "Open the Devin triage session, provide the requested input, then continue this run.",
                },
                raw_output=payload,
                devin_status=devin_status,
            )

        if devin_status in ACTIVE_DEVIN_STATUSES:
            return {
                "run_id": str(run["id"]),
                "action": "triage_still_running",
                "devin_status": devin_status,
                "status_detail": status_detail,
            }

        if devin_status == "error":
            fail_run(str(run["id"]), f"Triage session ended with status: {devin_status}")
            return {
                "run_id": str(run["id"]),
                "action": "triage_failed",
                "devin_status": devin_status,
            }

        if devin_status not in TERMINAL_DEVIN_STATUSES:
            return {
                "run_id": str(run["id"]),
                "action": "triage_waiting",
                "devin_status": devin_status,
            }

        return complete_triage(
            run=run,
            triage=extract_triage_json(payload),
            raw_output=payload,
            devin_status=devin_status,
        )

    except Exception as e:
        fail_run(str(run["id"]), str(e))
        return {
            "run_id": str(run["id"]),
            "action": "failed",
            "error": str(e),
        }


def complete_triage(
    run: JsonObject,
    triage: JsonObject,
    raw_output: JsonObject,
    devin_status: str | None,
) -> TransitionResult:
    decision = normalize_value(
        triage.get("decision"),
        allowed={"autofix", "needs_human", "ignore"},
        fallback="needs_human",
    )

    risk = normalize_value(
        triage.get("risk"),
        allowed={"low", "medium", "high", "hard"},
        fallback="medium",
    )

    category = normalize_value(
        triage.get("category"),
        allowed={"frontend", "backend", "test", "docs", "security", "unknown"},
        fallback="unknown",
    )

    if risk == "hard":
        return complete_hard_triage(
            run=run,
            triage=triage,
            raw_output=raw_output,
            devin_status=devin_status,
            decision=decision,
            risk=risk,
            category=category,
        )

    if decision == "autofix" and risk in {"low", "medium"}:
        next_status = "triage_completed"
    elif decision == "ignore":
        next_status = "ignored"
    else:
        next_status = "needs_human"

    fields: JsonObject = {
        "status": next_status,
        "triage_decision": decision,
        "triage_risk": risk,
        "triage_category": category,
        "triage_summary": as_optional_str(triage.get("reasoning_summary")),
        "triage_recommended_fix": as_optional_str(triage.get("recommended_fix")),
        "ui_change": bool(triage.get("ui_change_expected", False)),
        "raw_triage_output": raw_output,
        "error": None,
    }

    if next_status in {"needs_human", "ignored"}:
        fields["completed_at"] = utc_now()

    updated = transition_run(
        run_id=str(run["id"]),
        expected_status="triage_started",
        fields=fields,
        event_type=next_status,
        message=f"Triage completed: {decision} / {risk} / {category}",
        metadata={
            "triage": triage,
            "devin_status": devin_status,
        },
    )

    if not updated:
        return stale_transition(run, "complete_triage")

    return {
        "run_id": str(run["id"]),
        "action": "triage_completed",
        "next_status": next_status,
        "triage": triage,
    }


def complete_hard_triage(
    run: JsonObject,
    triage: JsonObject,
    raw_output: JsonObject,
    devin_status: str | None,
    decision: str,
    risk: str,
    category: str,
) -> TransitionResult:
    devin_start_url = build_devin_start_url(run)
    reasoning_summary = as_optional_str(triage.get("reasoning_summary"))
    recommended_fix = as_optional_str(triage.get("recommended_fix"))
    handoff_summary = build_hard_issue_handoff_summary(
        devin_start_url=devin_start_url,
        reasoning_summary=reasoning_summary,
        recommended_fix=recommended_fix,
    )

    updated = transition_run(
        run_id=str(run["id"]),
        expected_status="triage_started",
        fields={
            "status": "needs_human",
            "triage_decision": decision,
            "triage_risk": risk,
            "triage_category": category,
            "triage_summary": reasoning_summary,
            "triage_recommended_fix": handoff_summary,
            "ui_change": bool(triage.get("ui_change_expected", False)),
            "raw_triage_output": raw_output,
            "error": "Hard issue requires a human-in-the-loop Devin launch before remediation.",
            "completed_at": utc_now(),
        },
        event_type="hard_issue_needs_human",
        message="Triage rated this issue hard, so PatchOps stopped before remediation.",
        metadata={
            "triage": triage,
            "devin_status": devin_status,
            "devin_start_url": devin_start_url,
        },
    )

    if not updated:
        return stale_transition(run, "complete_hard_triage")

    comment_result = post_hard_issue_comment(
        run=run,
        triage=triage,
        devin_start_url=devin_start_url,
    )

    return {
        "run_id": str(run["id"]),
        "action": "hard_issue_needs_human",
        "next_status": "needs_human",
        "triage": triage,
        "devin_start_url": devin_start_url,
        "comment": comment_result,
    }


def post_hard_issue_comment(
    run: JsonObject,
    triage: JsonObject,
    devin_start_url: str,
) -> JsonObject:
    run_id = str(run["id"])
    issue_number = as_int(run.get("issue_number"))
    repository = github_repository_from_issue_url(as_optional_str(run.get("issue_url")))

    if issue_number is None or not repository:
        reason = "Missing GitHub repository or issue number for hard-issue comment."
        log_event(
            run_id=run_id,
            event_type="hard_issue_comment_skipped",
            message=reason,
            metadata={"devin_start_url": devin_start_url},
        )
        return {"posted": False, "reason": reason}

    try:
        comment = post_issue_comment(
            repository=repository,
            issue_number=issue_number,
            body=build_hard_issue_comment(run, triage, devin_start_url),
        )
    except GitHubAPIError as error:
        log_event(
            run_id=run_id,
            event_type="hard_issue_comment_failed",
            message=str(error),
            metadata={"devin_start_url": devin_start_url},
        )
        return {"posted": False, "reason": str(error)}

    comment_url = as_optional_str(comment.get("html_url"))
    log_event(
        run_id=run_id,
        event_type="hard_issue_comment_posted",
        message="Posted hard-issue human-in-the-loop handoff comment.",
        metadata={
            "comment_url": comment_url,
            "devin_start_url": devin_start_url,
        },
    )
    return {"posted": True, "comment_url": comment_url}


def build_hard_issue_comment(
    run: JsonObject,
    triage: JsonObject,
    devin_start_url: str,
) -> str:
    issue_title = as_optional_str(run.get("issue_title")) or "this issue"
    reasoning_summary = as_optional_str(triage.get("reasoning_summary")) or "Devin rated the issue as hard."
    recommended_fix = as_optional_str(triage.get("recommended_fix")) or "Have a human review scope before launching remediation."

    return (
        "## PatchOps recommends a human-in-the-loop Devin run\n\n"
        f"Devin triaged **{issue_title}** as **hard**. This could become a large run, "
        "so PatchOps is not starting autonomous remediation automatically.\n\n"
        f"**Why:** {reasoning_summary}\n\n"
        f"**Suggested next step:** {recommended_fix}\n\n"
        "A human should review the scope, then intentionally start or continue the issue in Devin:\n\n"
        f"**[Start this issue in Devin]({devin_start_url})**"
    )


def build_hard_issue_handoff_summary(
    devin_start_url: str,
    reasoning_summary: str | None,
    recommended_fix: str | None,
) -> str:
    parts = [
        "PatchOps stopped before remediation because Devin rated this issue as hard.",
        "This could become a large run, so a human should review the scope before launching Devin.",
        f"Start this issue in Devin: {devin_start_url}",
    ]

    if reasoning_summary:
        parts.append(f"Triage summary: {reasoning_summary}")

    if recommended_fix:
        parts.append(f"Suggested next step: {recommended_fix}")

    return "\n\n".join(parts)


def build_devin_start_url(run: JsonObject) -> str:
    return (
        as_optional_str(run.get("triage_session_url"))
        or as_optional_str(run.get("remediation_session_url"))
        or DEVIN_APP_URL
    )


def github_repository_from_issue_url(issue_url: str | None) -> str | None:
    if not issue_url:
        return None

    parsed = urlparse(issue_url)
    parts = [part for part in parsed.path.split("/") if part]

    if parsed.netloc != "github.com" or len(parts) < 2:
        return None

    return f"{parts[0]}/{parts[1]}"


def start_remediation(run: JsonObject) -> TransitionResult:
    """
    Continues remediation in the SAME Devin triage session.

    Once triage returns its verdict, the session sits "awaiting instructions".
    We send a "proceed with the fix" follow-up so Devin implements its own
    recommended fix and opens a PR without spinning up a separate session.
    """

    try:
        session_id = as_optional_str(run.get("triage_session_id"))

        if not session_id:
            raise ValueError("Cannot start remediation without triage_session_id")

        session_url = as_optional_str(run.get("triage_session_url"))

        updated = transition_run(
            run_id=str(run["id"]),
            expected_status="triage_completed",
            fields={
                "status": "remediation_started",
                "remediation_session_id": session_id,
                "remediation_session_url": session_url,
                "error": None,
            },
            event_type="remediation_started",
            message=f"Sent 'proceed with the fix' to Devin session for issue #{run['issue_number']}",
            metadata={
                "session_id": session_id,
                "session_url": session_url,
                "continued_triage_session": True,
            },
        )

        if not updated:
            return stale_transition(run, "start_remediation")

        send_session_message(
            session_id=session_id,
            message=remediation_followup_prompt(),
        )

        return {
            "run_id": str(run["id"]),
            "action": "start_remediation",
            "session_id": session_id,
            "session_url": session_url,
        }

    except Exception as e:
        fail_run(str(run["id"]), str(e))
        return {
            "run_id": str(run["id"]),
            "action": "failed",
            "error": str(e),
        }


def poll_remediation(run: JsonObject) -> TransitionResult:
    try:
        session_id = as_optional_str(run.get("remediation_session_id"))

        if not session_id:
            raise ValueError("Missing remediation_session_id")

        session = get_session_snapshot(session_id)
        messages = get_session_messages(session_id)
        payload = {"session": session, "messages": messages}
        outcome = maybe_extract_remediation_json(payload)
        devin_status = as_optional_str(session.get("status"))
        status_detail = normalize_session_detail(session.get("status_detail"))

        if outcome:
            return complete_remediation(
                run=run,
                outcome=outcome,
                raw_output=payload,
                devin_status=devin_status,
            )

        if status_detail in WAITING_DETAILS:
            return {
                "run_id": str(run["id"]),
                "action": "remediation_waiting",
                "devin_status": devin_status,
                "status_detail": status_detail,
            }

        if devin_status in ACTIVE_DEVIN_STATUSES:
            return {
                "run_id": str(run["id"]),
                "action": "remediation_still_running",
                "devin_status": devin_status,
                "status_detail": status_detail,
            }

        if devin_status in TERMINAL_DEVIN_STATUSES:
            return complete_remediation(
                run=run,
                outcome={
                    "status": "failed",
                    "summary": "Could not parse PR or blocker details from terminal remediation output.",
                    "tests_run": [],
                },
                raw_output=payload,
                devin_status=devin_status,
            )

        return {
            "run_id": str(run["id"]),
            "action": "remediation_waiting",
            "devin_status": devin_status,
        }

    except Exception as e:
        fail_run(str(run["id"]), str(e))
        return {
            "run_id": str(run["id"]),
            "action": "failed",
            "error": str(e),
        }


def complete_remediation(
    run: JsonObject,
    outcome: JsonObject,
    raw_output: JsonObject,
    devin_status: str | None,
) -> TransitionResult:
    explicit_status = as_optional_str(outcome.get("status"))
    status = normalize_value(
        explicit_status,
        allowed={"pr_created", "blocked", "failed"},
        fallback="failed",
    )
    pr_url = as_optional_str(outcome.get("pr_url"))
    tests_run = normalize_string_list(outcome.get("tests_run"))
    video_url = as_optional_str(outcome.get("video_url")) or extract_video_url(raw_output)
    summary = as_optional_str(outcome.get("summary")) or "No remediation summary provided."

    if pr_url and explicit_status != "failed":
        status = "pr_created"

    if status == "pr_created":
        next_status = "pr_created"
        event_type = "pr_created"
        error = None
        completed_at = None
    elif status == "blocked":
        next_status = "needs_human"
        event_type = "remediation_blocked"
        error = as_optional_str(outcome.get("blocker")) or summary
        completed_at = utc_now()
    else:
        next_status = "failed"
        event_type = "remediation_failed"
        error = as_optional_str(outcome.get("error")) or summary
        completed_at = utc_now()

    fields: JsonObject = {
        "status": next_status,
        "pr_url": pr_url,
        "ui_change": bool(outcome.get("ui_change", run.get("ui_change", False))),
        "tests_run": tests_run,
        "video_url": video_url,
        "summary": summary,
        "raw_remediation_output": raw_output,
        "error": error,
    }

    if completed_at:
        fields["completed_at"] = completed_at

    updated = transition_run(
        run_id=str(run["id"]),
        expected_status="remediation_started",
        fields=fields,
        event_type=event_type,
        message=summary,
        metadata={
            "outcome": outcome,
            "devin_status": devin_status,
            "video_available": bool(video_url),
        },
    )

    if not updated:
        return stale_transition(run, "complete_remediation")

    return {
        "run_id": str(run["id"]),
        "action": event_type,
        "next_status": next_status,
        "pr_url": pr_url,
        "video_url": video_url,
    }


def start_review(run: JsonObject) -> TransitionResult:
    try:
        pr_url = as_optional_str(run.get("pr_url"))

        if not pr_url:
            raise ValueError("Cannot request Devin Review without pr_url")

        review = request_devin_review(pr_url)
        review_id = (
            as_optional_str(review.get("commit_sha"))
            or as_optional_str(review.get("session_id"))
            or as_optional_str(review.get("review_id"))
            or as_optional_str(review.get("id"))
        )

        if not review_id:
            raise ValueError("Devin Review response did not include a review identifier")

        updated = transition_run(
            run_id=str(run["id"]),
            expected_status="pr_created",
            fields={
                "status": "review_started",
                "review_status": "requested",
                "review_session_id": review_id,
                "review_session_url": as_optional_str(review.get("url")),
                "raw_review_output": review,
                "error": None,
            },
            event_type="review_started",
            message="Requested Devin Review for the PR.",
            metadata={"review": review},
        )

        if not updated:
            return stale_transition(run, "start_review")

        return {
            "run_id": str(run["id"]),
            "action": "review_started",
            "review_id": review_id,
        }

    except Exception as e:
        fail_run(str(run["id"]), str(e))
        return {
            "run_id": str(run["id"]),
            "action": "failed",
            "error": str(e),
        }


def poll_review(run: JsonObject) -> TransitionResult:
    try:
        review_id = as_optional_str(run.get("review_session_id"))

        if not review_id:
            raise ValueError("Missing review_session_id")

        pr_url = as_optional_str(run.get("pr_url"))
        if not pr_url:
            raise ValueError("Missing pr_url")

        review = get_pr_review(pr_url, review_id)
        review_status = normalize_review_status(review)

        if review_status in {"queued", "running", "requested", "pending"}:
            return {
                "run_id": str(run["id"]),
                "action": "review_still_running",
                "review_status": review_status,
            }

        if review_status in {"failed", "error"}:
            return fail_review(run, review, review_status)

        archived_session = archive_run_devin_session(run)
        updated = transition_run(
            run_id=str(run["id"]),
            expected_status="review_started",
            fields={
                "status": "review_completed",
                "review_status": review_status,
                "raw_review_output": review,
                "error": None,
            },
            event_type="review_completed",
            message=f"Devin Review completed with status: {review_status}",
            metadata={
                "review": review,
                "archived_session": archived_session,
            },
        )

        if not updated:
            return stale_transition(run, "poll_review")

        return {
            "run_id": str(run["id"]),
            "action": "review_completed",
            "review_status": review_status,
        }

    except Exception as e:
        fail_run(str(run["id"]), str(e))
        return {
            "run_id": str(run["id"]),
            "action": "failed",
            "error": str(e),
        }


def archive_run_devin_session(run: JsonObject) -> JsonObject | None:
    session_id = (
        as_optional_str(run.get("remediation_session_id"))
        or as_optional_str(run.get("triage_session_id"))
    )

    if not session_id:
        return None

    return archive_session(session_id)


def fail_review(run: JsonObject, review: JsonObject, review_status: str) -> TransitionResult:
    message = as_optional_str(review.get("summary")) or f"Devin Review failed: {review_status}"
    updated = transition_run(
        run_id=str(run["id"]),
        expected_status="review_started",
        fields={
            "status": "failed",
            "review_status": review_status,
            "raw_review_output": review,
            "error": message,
            "completed_at": utc_now(),
        },
        event_type="review_failed",
        message=message,
        metadata={"review": review},
    )

    if not updated:
        return stale_transition(run, "fail_review")

    return {
        "run_id": str(run["id"]),
        "action": "review_failed",
        "review_status": review_status,
    }


def complete_report(run: JsonObject) -> TransitionResult:
    report = build_report(run)
    updated = transition_run(
        run_id=str(run["id"]),
        expected_status="review_completed",
        fields={
            "status": "report_ready",
            "summary": as_optional_str(report.get("summary")),
            "completed_at": utc_now(),
            "error": None,
        },
        event_type="report_ready",
        message="Local Patch Adams report is ready.",
        metadata={"report": report},
    )

    if not updated:
        return stale_transition(run, "complete_report")

    return {
        "run_id": str(run["id"]),
        "action": "report_ready",
        "report": report,
    }


def build_report(run: JsonObject) -> JsonObject:
    ui_change = bool(run.get("ui_change", False))
    verification_note = (
        "UI verification should be added before customer deployment."
        if ui_change
        else "No UI verification required for this non-UI change."
    )
    review_status = as_optional_str(run.get("review_status")) or "unknown"
    summary = (
        f"Policy gate: {run.get('triage_decision', 'unknown')} "
        f"/ {run.get('triage_risk', 'unknown')}; PR: {run.get('pr_url') or 'unavailable'}; "
        f"Devin Review: {review_status}."
    )

    return {
        "summary": summary,
        "trigger_source": "GitHub issue label: devin",
        "policy_gate": f"{run.get('triage_decision', 'unknown')} / {run.get('triage_risk', 'unknown')}",
        "human_stop_condition": run.get("error") or "Hard/high risk, ambiguity, blocked remediation, or review failure.",
        "time_to_pr": "Derived from run_events timestamps in the dashboard.",
        "tests_run": cast(JsonValue, run.get("tests_run") or []),
        "pr_url": run.get("pr_url"),
        "triage_session_url": run.get("triage_session_url"),
        "remediation_session_url": run.get("remediation_session_url"),
        "review_status": review_status,
        "video_url": run.get("video_url"),
        "patch_quality_evidence": run.get("summary") or "See PR, tests, and Devin Review output.",
        "known_limitations": [
            verification_note,
            "Production use should add authenticated webhooks, stronger RLS policies, and queue-backed workers.",
        ],
        "safety_rationale": "The control plane gates Devin with triage, status polling, event history, review, and terminal human-stop states.",
    }


def extract_triage_json(messages: JsonValue) -> JsonObject:
    """
    Returns a triage object if found; otherwise returns a safe needs_human fallback.
    """

    triage = maybe_extract_triage_json(messages)

    if triage:
        return triage

    return {
        "decision": "needs_human",
        "risk": "medium",
        "category": "unknown",
        "ui_change_expected": False,
        "reasoning_summary": "Could not parse structured triage output from Devin messages.",
        "recommended_fix": "Human review needed.",
    }


def maybe_extract_triage_json(messages: JsonValue) -> JsonObject | None:
    """
    Devin message response shape may vary.

    This tries, in order:
    1. Direct nested dict with decision/risk.
    2. Fenced ```json blocks.
    3. JSON-looking object containing "decision".
    """

    direct = find_direct_triage_object(messages)
    if direct:
        return direct

    for candidate in extract_json_candidates(messages, "decision"):
        parsed = try_parse_json_object(candidate)
        if is_triage_object(parsed):
            return parsed

    return None


def maybe_extract_remediation_json(payload: JsonValue) -> JsonObject | None:
    direct = find_direct_remediation_object(payload)
    if direct:
        return direct

    pull_request = find_pull_request_url(payload)
    if pull_request:
        return {
            "status": "pr_created",
            "pr_url": pull_request,
            "ui_change": False,
            "tests_run": [],
            "summary": "PR URL was available from Devin session metadata; structured test and summary fields were not provided.",
            "video_url": extract_video_url(payload),
        }

    for candidate in extract_json_candidates(payload, "status"):
        parsed = try_parse_json_object(candidate)
        if is_remediation_object(parsed):
            return parsed

    return None


def find_direct_triage_object(value: JsonValue) -> JsonObject | None:
    """
    Walk arbitrary nested Devin message payload looking for a triage dict.
    """

    if isinstance(value, dict):
        if is_triage_object(value):
            return cast(JsonObject, value)

        for child in value.values():
            found = find_direct_triage_object(child)
            if found:
                return found

    if isinstance(value, list):
        for item in value:
            found = find_direct_triage_object(item)
            if found:
                return found

    return None


def find_direct_remediation_object(value: JsonValue) -> JsonObject | None:
    if isinstance(value, dict):
        if is_remediation_object(value):
            return cast(JsonObject, value)

        structured_output = value.get("structured_output")
        if isinstance(structured_output, dict) and is_remediation_object(structured_output):
            return cast(JsonObject, structured_output)

        for child in value.values():
            found = find_direct_remediation_object(child)
            if found:
                return found

    if isinstance(value, list):
        for item in value:
            found = find_direct_remediation_object(item)
            if found:
                return found

    return None


def is_triage_object(value: JsonValue) -> bool:
    return (
        isinstance(value, dict)
        and "decision" in value
        and "risk" in value
    )


def is_remediation_object(value: JsonValue) -> bool:
    return (
        isinstance(value, dict)
        and (
            "pr_url" in value
            or value.get("status") in {"pr_created", "blocked", "failed"}
        )
    )


def maybe_extract_review_json(payload: JsonValue) -> JsonObject | None:
    direct = find_direct_review_object(payload)
    if direct:
        return direct

    for candidate in extract_json_candidates(payload, "summary"):
        parsed = try_parse_json_object(candidate)
        if is_review_object(parsed):
            return parsed

    return None


def extract_review_json(payload: JsonValue) -> JsonObject:
    review = maybe_extract_review_json(payload)
    if review:
        return review

    return {
        "status": "completed",
        "summary": "Devin session ended without structured review JSON; see session messages for details.",
    }


def find_direct_review_object(value: JsonValue) -> JsonObject | None:
    if isinstance(value, dict):
        if is_review_object(value):
            return cast(JsonObject, value)

        structured_output = value.get("structured_output")
        if isinstance(structured_output, dict) and is_review_object(structured_output):
            return cast(JsonObject, structured_output)

        for child in value.values():
            found = find_direct_review_object(child)
            if found:
                return found

    if isinstance(value, list):
        for item in value:
            found = find_direct_review_object(item)
            if found:
                return found

    return None


def is_review_object(value: JsonValue) -> bool:
    if not isinstance(value, dict):
        return False

    status = value.get("status")
    if not isinstance(status, str):
        return False

    return status.strip().lower() in {
        "completed",
        "passed",
        "changes_requested",
        "failed",
        "error",
    }


def extract_json_candidates(value: JsonValue, required_key: str) -> list[str]:
    candidates: list[str] = []

    for text in extract_text_values(value):
        candidates.extend(extract_json_candidates_from_text(text, required_key))

    candidates.extend(
        extract_json_candidates_from_text(json.dumps(value, default=str), required_key)
    )

    return list(reversed(candidates))


def extract_json_candidates_from_text(text: str, required_key: str) -> list[str]:
    candidates: list[str] = []
    candidates.extend(
        re.findall(
            r"```(?:json)?\s*(\{.*?\})\s*```",
            text,
            re.DOTALL,
        )
    )
    candidates.extend(
        re.findall(
            rf"\{{[^{{}}]*\"{re.escape(required_key)}\"[^{{}}]*\}}",
            text,
            re.DOTALL,
        )
    )
    return candidates


def extract_text_values(value: JsonValue) -> list[str]:
    if isinstance(value, str):
        return [value]

    if isinstance(value, dict):
        texts: list[str] = []
        for child in value.values():
            texts.extend(extract_text_values(child))
        return texts

    if isinstance(value, list):
        texts: list[str] = []
        for item in value:
            texts.extend(extract_text_values(item))
        return texts

    return []


def try_parse_json_object(value: str) -> JsonObject | None:
    try:
        parsed = json.loads(value)
        if isinstance(parsed, dict):
            return cast(JsonObject, parsed)
        return None
    except json.JSONDecodeError:
        return None


def normalize_value(
    value: JsonValue,
    allowed: set[str],
    fallback: str,
) -> str:
    if not isinstance(value, str):
        return fallback

    normalized = value.strip().lower()

    if normalized in allowed:
        return normalized

    return fallback


def normalize_string_list(value: JsonValue) -> list[str]:
    if not isinstance(value, list):
        return []

    return [str(item) for item in value if isinstance(item, str)]


def normalize_session_detail(value: JsonValue) -> str | None:
    if isinstance(value, str):
        return value.strip().lower()

    if isinstance(value, dict):
        for key in ("state", "status", "detail"):
            detail = as_optional_str(value.get(key))
            if detail:
                return detail.strip().lower()

    return None


def normalize_review_status(review: JsonObject) -> str:
    for key in ("status", "state", "conclusion"):
        value = normalize_value(
            review.get(key),
            allowed={
                "requested",
                "queued",
                "pending",
                "running",
                "completed",
                "passed",
                "changes_requested",
                "failed",
                "error",
                "errored",
            },
            fallback="",
        )
        if value:
            if value == "pending":
                return "requested"
            if value == "errored":
                return "error"
            return value

    return "completed"


def find_pull_request_url(value: JsonValue) -> str | None:
    if isinstance(value, dict):
        for key in ("pr_url", "url", "html_url"):
            candidate = as_optional_str(value.get(key))
            if candidate and "/pull/" in candidate:
                return candidate

        pull_requests = value.get("pull_requests")
        if isinstance(pull_requests, list):
            for pull_request in pull_requests:
                found = find_pull_request_url(pull_request)
                if found:
                    return found

        for child in value.values():
            found = find_pull_request_url(child)
            if found:
                return found

    if isinstance(value, list):
        for item in value:
            found = find_pull_request_url(item)
            if found:
                return found

    if isinstance(value, str):
        match = re.search(r"https://github\.com/[^\s\"'<>]+/pull/\d+", value)
        if match:
            return match.group(0)

    return None


def extract_video_url(value: JsonValue) -> str | None:
    if isinstance(value, dict):
        for key in ("video_url", "recording_url", "attachment_url", "url"):
            candidate = as_optional_str(value.get(key))
            if candidate and is_video_url(candidate):
                return candidate

        for child in value.values():
            found = extract_video_url(child)
            if found:
                return found

    if isinstance(value, list):
        for item in value:
            found = extract_video_url(item)
            if found:
                return found

    if isinstance(value, str):
        for match in re.findall(r"https?://[^\s\"'<>]+", value):
            if is_video_url(match):
                return match

    return None


def extract_waiting_instruction(value: JsonValue) -> str | None:
    for text in reversed(extract_text_values(value)):
        normalized = text.strip()
        if (
            normalized
            and normalized.lower() not in WAITING_DETAILS
            and "waiting" in normalized.lower()
        ):
            return normalized

    return None


def is_video_url(value: str) -> bool:
    lowered = value.lower()
    return any(token in lowered for token in ("recording", "video", ".webm", ".mp4", ".mov"))


def as_optional_str(value: JsonValue) -> str | None:
    if isinstance(value, str) and value.strip():
        return value

    return None


def as_int(value: JsonValue) -> int | None:
    if isinstance(value, int):
        return value

    return None


def stale_transition(run: JsonObject, action: str) -> TransitionResult:
    return {
        "run_id": str(run["id"]),
        "action": action,
        "status": str(run.get("status")),
        "stale": True,
    }