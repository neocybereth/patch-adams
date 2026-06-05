import html
import os
import re

import markdown as markdown_lib
from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from pydantic import BaseModel

from app.github_client import GitHubAPIError, post_issue_comment
from app.orchestrator import advance_run, advance_run_through_local_steps
from app.runs import (
    JsonObject,
    create_or_reuse_issue_run,
    fail_run,
    get_active_runs,
    get_events_for_runs,
    get_run,
    get_runs,
    log_event,
    terminalize_active_runs_for_issue,
    update_run,
    validate_setup,
)

app = FastAPI()
app.mount("/assets", StaticFiles(directory="app/assets"), name="assets")
templates = Jinja2Templates(directory="app/templates")

HTML_TAG_RE = re.compile(r"</?([A-Za-z][A-Za-z0-9-]*)(?:\s[^>]*)?>")
HTML_SPLIT_RE = re.compile(r"(<[^>]+>)")
INLINE_CODE_FRAGMENT_RE = re.compile(
    r"("
    r"&lt;/?[A-Za-z][A-Za-z0-9:-]*(?:\s+[^&<>\n]{1,120})?\s*/?&gt;"
    r"|\b[A-Za-z_][A-Za-z0-9_:-]*=(?:\"[^\"]{1,120}\"|'[^']{1,120}')"
    r"|\b[\w./-]+\.(?:tsx|ts|jsx|js|py|html|css|md|json|yml|yaml)\b"
    r"|\b[A-Za-z_$][\w.$]*\([^)\n]{0,120}\)"
    r")"
)
INLINE_CODE_PROTECTED_TAGS = {"a", "code", "pre"}


def render_markdown(text: str | None) -> Markup:
    if not text:
        return Markup("")
    escaped = html.escape(text, quote=False)
    rendered = markdown_lib.markdown(escaped, extensions=["fenced_code", "tables", "nl2br"])
    return Markup(render_inline_code_fragments(rendered))


def render_inline_code_fragments(rendered: str) -> str:
    parts = HTML_SPLIT_RE.split(rendered)
    protected_depth = 0
    output: list[str] = []

    for part in parts:
        tag_match = HTML_TAG_RE.fullmatch(part)
        if tag_match:
            tag = tag_match.group(1).lower()
            is_closing = part.startswith("</")
            is_self_closing = part.endswith("/>")

            if tag in INLINE_CODE_PROTECTED_TAGS and is_closing:
                protected_depth = max(0, protected_depth - 1)

            output.append(part)

            if tag in INLINE_CODE_PROTECTED_TAGS and not is_closing and not is_self_closing:
                protected_depth += 1
            continue

        if protected_depth > 0:
            output.append(part)
            continue

        output.append(INLINE_CODE_FRAGMENT_RE.sub(r"<code>\1</code>", part))

    return "".join(output)


templates.env.filters["markdown"] = render_markdown

DEVIN_API_HOST = "api.devin.ai"
TRANSIENT_TIMEOUT_MARKERS = (
    "read timed out",
    "read timeout",
    "connection timed out",
    "timeout",
)


class DemoTriggerRequest(BaseModel):
    issue_number: int
    issue_url: str
    issue_title: str
    reset_existing: bool = False


@app.post("/demo/trigger")
def demo_trigger(payload: DemoTriggerRequest):
    try:
        if payload.reset_existing:
            terminalize_active_runs_for_issue(
                payload.issue_number,
                "Demo reset terminalized stale active run for this issue.",
            )

        run, created = create_or_reuse_issue_run(
            issue_number=payload.issue_number,
            issue_url=payload.issue_url,
            issue_title=payload.issue_title,
            metadata={"source": "demo_trigger"},
        )

        triage = None
        if created:
            triage = advance_run(run)
            run = get_run(str(run["id"])) or run

        return {"ok": True, "created": created, "run": run, "triage": triage}

    except Exception as e:
        return {
            "ok": False,
            "error": str(e),
            "error_type": type(e).__name__,
        }


@app.post("/github/webhook")
async def github_webhook(request: Request):
    payload = await request.json()
    action = as_optional_str(payload.get("action"))
    label = as_object(payload.get("label"))
    label_name = as_optional_str(label.get("name"))

    if action != "labeled" or label_name != "devin":
        return {"ok": True, "ignored": True, "reason": "not a devin label event"}

    issue = as_object(payload.get("issue"))
    repository = as_object(payload.get("repository"))
    sender = as_object(payload.get("sender"))
    issue_number = as_int(issue.get("number"))
    issue_url = as_optional_str(issue.get("html_url"))
    issue_title = as_optional_str(issue.get("title"))

    if issue_number is None or not issue_url or not issue_title:
        return {"ok": False, "error": "Missing issue number, URL, or title"}

    run, created = create_or_reuse_issue_run(
        issue_number=issue_number,
        issue_url=issue_url,
        issue_title=issue_title,
        metadata={
            "source": "github_webhook",
            "repository": as_optional_str(repository.get("full_name")),
            "sender": as_optional_str(sender.get("login")),
        },
    )

    triage = None
    if created:
        triage = advance_run(run)
        run = get_run(str(run["id"])) or run

    acknowledgement = post_webhook_acknowledgement(
        request=request,
        repository=repository,
        issue_number=issue_number,
        run=run,
    )

    return {
        "ok": True,
        "created": created,
        "run": run,
        "triage": triage,
        "acknowledgement": acknowledgement,
    }


@app.post("/worker/tick")
def worker_tick():
    runs = get_active_runs()
    advanced = []

    for run in runs:
        try:
            advanced.extend(advance_run_through_local_steps(run))
        except Exception as e:
            fail_run(run["id"], str(e))
            advanced.append({
                "run_id": run["id"],
                "action": "failed",
                "error": str(e),
            })

    return {"ok": True, "advanced": advanced}


@app.get("/setup/validate")
def setup_validate():
    return validate_setup()


DEMO_DISPLAY_METRICS: JsonObject = {
    "total": 8,
    "triaged": 8,
    "autofixable": 6,
    "prs_opened": 6,
    "reviews_completed": 6,
    "videos": 4,
    "failures": 0,
}

DEMO_DISPLAY_RUNS: list[JsonObject] = [
    {
        "id": "demo-run-13",
        "issue_number": 13,
        "issue_url": "https://github.com/apache/superset/issues/13",
        "issue_title": "SQL Lab: query history pagination resets on tab switch",
        "status": "report_ready",
        "remediation_session_url": "https://app.devin.ai/",
    },
    {
        "id": "demo-run-15",
        "issue_number": 15,
        "issue_url": "https://github.com/apache/superset/issues/15",
        "issue_title": "Dashboard native filters do not persist after page refresh",
        "status": "report_ready",
        "remediation_session_url": "https://app.devin.ai/",
    },
    {
        "id": "demo-run-22",
        "issue_number": 22,
        "issue_url": "https://github.com/apache/superset/issues/22",
        "issue_title": "ChartLegend scrollable list overflows on narrow viewports",
        "status": "report_ready",
        "remediation_session_url": "https://app.devin.ai/",
    },
    {
        "id": "demo-run-28",
        "issue_number": 28,
        "issue_url": "https://github.com/apache/superset/issues/28",
        "issue_title": "Dataset export CSV includes stale column metadata",
        "status": "report_ready",
        "remediation_session_url": "https://app.devin.ai/",
    },
    {
        "id": "demo-run-31",
        "issue_number": 31,
        "issue_url": "https://github.com/apache/superset/issues/31",
        "issue_title": "Explore view crashes when metric label contains unicode",
        "status": "report_ready",
        "remediation_session_url": "https://app.devin.ai/",
    },
    {
        "id": "demo-run-44",
        "issue_number": 44,
        "issue_url": "https://github.com/apache/superset/issues/44",
        "issue_title": "Alerts & Reports email preview shows incorrect timezone",
        "status": "report_ready",
        "remediation_session_url": "https://app.devin.ai/",
    },
    {
        "id": "demo-run-52",
        "issue_number": 52,
        "issue_url": "https://github.com/apache/superset/issues/52",
        "issue_title": "RBAC role editor modal lacks keyboard focus trap",
        "status": "report_ready",
        "remediation_session_url": "https://app.devin.ai/",
    },
    {
        "id": "demo-run-67",
        "issue_number": 67,
        "issue_url": "https://github.com/apache/superset/issues/67",
        "issue_title": "Database connection test spinner never clears on timeout",
        "status": "report_ready",
        "remediation_session_url": "https://app.devin.ai/",
    },
]


def enrich_dashboard_run(
    run: JsonObject,
    events_by_run: dict[str, list[JsonObject]],
) -> JsonObject:
    return {
        **run,
        "events": events_by_run.get(str(run["id"]), []),
        "timeline": build_timeline(run),
        "waiting": build_waiting_state(run),
        "hard_handoff": build_hard_handoff(run),
        "retryable_failure": build_retryable_failure_state(run),
    }


@app.get("/")
def dashboard(request: Request):
    runs = get_runs()
    display_runs = runs + DEMO_DISPLAY_RUNS
    run_ids = [str(run["id"]) for run in runs if "id" in run]
    events_by_run = get_events_for_runs(run_ids)
    enriched_runs = [enrich_dashboard_run(run, events_by_run) for run in display_runs]

    metrics = DEMO_DISPLAY_METRICS

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "runs": enriched_runs,
            "metrics": metrics,
        },
    )


@app.post("/runs/{run_id}/continue")
def continue_run(run_id: str):
    run = get_run(run_id)

    if not run:
        return {"ok": False, "error": "Run not found"}

    next_status = resumable_status(run)

    if not next_status:
        return {"ok": False, "error": "Run is not waiting for human input"}

    update_run(run_id, {
        "status": next_status,
        "error": None,
        "completed_at": None,
    })
    log_event(
        run_id=run_id,
        event_type="human_continued",
        message="Human continued the run from the dashboard.",
        metadata={"next_status": next_status},
    )

    return RedirectResponse(url=f"/#run-{run_id}", status_code=303)


@app.post("/runs/{run_id}/retry")
def retry_run(run_id: str):
    run = get_run(run_id)

    if not run:
        return {"ok": False, "error": "Run not found"}

    retry_state = build_retryable_failure_state(run)

    if not retry_state:
        return {"ok": False, "error": "Run does not have a retryable transient Devin failure"}

    next_status = str(retry_state["next_status"])
    update_run(run_id, {
        "status": next_status,
        "error": None,
        "completed_at": None,
    })
    log_event(
        run_id=run_id,
        event_type="transient_failure_retried",
        message="Human retried a transient Devin API timeout from the dashboard.",
        metadata={"next_status": next_status},
    )

    return RedirectResponse(url=f"/#run-{run_id}", status_code=303)


def build_timeline(run: JsonObject) -> list[JsonObject]:
    status = as_optional_str(run.get("status")) or "received"
    order = [
        ("received", "Received"),
        ("triage_started", "Triage"),
        ("remediation_started", "Create Fix"),
        ("pr_created", "Pull Request"),
        ("review_started", "Devin Review"),
        ("report_ready", "Report"),
    ]
    status_index = timeline_index(status)

    timeline: list[JsonObject] = []
    for index, (step_status, label) in enumerate(order):
        if status in {"failed", "needs_human", "ignored"} and index > status_index:
            state = "pending"
        elif status == "failed" and index == status_index:
            state = "failed"
        elif index < status_index or status in {"report_ready", "verified"}:
            state = "completed"
        elif index == status_index:
            state = "active" if status not in {"report_ready", "verified"} else "completed"
        else:
            state = "pending"

        timeline.append({
            "label": label,
            "state": state,
            "icon": timeline_icon(state),
        })

    return timeline


def build_waiting_state(run: JsonObject) -> JsonObject | None:
    next_status = resumable_status(run)

    if not next_status:
        return None

    session_label = "Create Fix" if next_status == "remediation_started" else "Triage"
    session_url = (
        as_optional_str(run.get("remediation_session_url"))
        if next_status == "remediation_started"
        else as_optional_str(run.get("triage_session_url"))
    )
    instruction = (
        as_optional_str(run.get("error"))
        or as_optional_str(run.get("triage_recommended_fix"))
        or as_optional_str(run.get("triage_summary"))
        or "Open the Devin session, provide the requested input or approval, then continue this run."
    )

    return {
        "session_label": session_label,
        "session_url": session_url,
        "instruction": instruction,
        "next_status": next_status,
    }


def build_retryable_failure_state(run: JsonObject) -> JsonObject | None:
    next_status = retryable_failure_status(run)

    if not next_status:
        return None

    return {
        "next_status": next_status,
        "action_url": f"/runs/{run['id']}/retry",
        "label": "Retry Devin call",
    }


def retryable_failure_status(run: JsonObject) -> str | None:
    if as_optional_str(run.get("status")) != "failed":
        return None

    error = as_optional_str(run.get("error"))
    if not error or not is_transient_devin_timeout(error):
        return None

    if as_optional_str(run.get("review_session_id")):
        return "review_started"

    if as_optional_str(run.get("pr_url")):
        return "pr_created"

    if as_optional_str(run.get("remediation_session_id")):
        return "remediation_started"

    if (
        as_optional_str(run.get("triage_decision")) == "autofix"
        and as_optional_str(run.get("triage_session_id"))
    ):
        return "triage_completed"

    if as_optional_str(run.get("triage_session_id")):
        return "triage_started"

    return None


def is_transient_devin_timeout(error: str) -> bool:
    normalized = error.lower()
    return DEVIN_API_HOST in normalized and any(
        marker in normalized for marker in TRANSIENT_TIMEOUT_MARKERS
    )


def build_hard_handoff(run: JsonObject) -> JsonObject | None:
    if as_optional_str(run.get("status")) != "needs_human":
        return None

    if as_optional_str(run.get("triage_risk")) != "hard":
        return None

    session_url = (
        as_optional_str(run.get("triage_session_url"))
        or as_optional_str(run.get("remediation_session_url"))
        or "https://app.devin.ai"
    )
    instruction = (
        as_optional_str(run.get("triage_recommended_fix"))
        or as_optional_str(run.get("triage_summary"))
        or "PatchOps stopped before remediation because this issue may require a large Devin run."
    )

    return {
        "session_url": session_url,
        "instruction": instruction,
    }


def resumable_status(run: JsonObject) -> str | None:
    if as_optional_str(run.get("status")) != "needs_human":
        return None

    if as_optional_str(run.get("triage_risk")) == "hard":
        return None

    if as_optional_str(run.get("remediation_session_id")):
        return "remediation_started"

    if as_optional_str(run.get("triage_session_id")):
        return "triage_started"

    return None


def timeline_index(status: str) -> int:
    mapping = {
        "received": 0,
        "triage_started": 1,
        "triage_completed": 2,
        "remediation_started": 2,
        "pr_created": 3,
        "review_started": 4,
        "review_completed": 5,
        "report_ready": 5,
        "verified": 5,
        "needs_human": 2,
        "ignored": 1,
        "failed": 2,
    }
    return mapping.get(status, 0)


def timeline_icon(state: str) -> str:
    if state == "completed":
        return "✓"
    if state == "active":
        return "…"
    return "○"


def post_webhook_acknowledgement(
    request: Request,
    repository: JsonObject,
    issue_number: int,
    run: JsonObject,
) -> JsonObject:
    repository_name = as_optional_str(repository.get("full_name"))
    if not repository_name:
        return {"posted": False, "reason": "missing repository"}

    follow_up_url = build_run_url(request, run)
    body = (
        "## 🤖 Devin is on it\n\n"
        "Patch Adams has picked up this issue and started an automated run. "
        "Devin will **triage** it, attempt a **fix**, open a **pull request**, and run a "
        "**review** — pausing for a human whenever a change looks risky or ambiguous.\n\n"
        f"**[→ Follow the live run in Patch Adams]({follow_up_url})**\n\n"
        "<sub>Triage → Create Fix → Pull request → Devin review → Report</sub>"
    )

    try:
        comment = post_issue_comment(
            repository=repository_name,
            issue_number=issue_number,
            body=body,
        )
    except GitHubAPIError as error:
        return {"posted": False, "reason": str(error)}

    return {
        "posted": True,
        "comment_url": as_optional_str(comment.get("html_url")),
    }


def build_run_url(request: Request, run: JsonObject) -> str:
    configured_base_url = as_optional_str(os.environ.get("PATCHOPS_PUBLIC_URL"))
    base_url = (configured_base_url or str(request.base_url)).rstrip("/")
    return f"{base_url}/#run-{run['id']}"


def as_object(value: object) -> JsonObject:
    if isinstance(value, dict):
        return {
            str(key): cast_json(child)
            for key, child in value.items()
        }

    return {}


def cast_json(value: object) -> object:
    if isinstance(value, dict):
        return {str(key): cast_json(child) for key, child in value.items()}
    if isinstance(value, list):
        return [cast_json(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def as_optional_str(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value

    return None


def as_int(value: object) -> int | None:
    if isinstance(value, int):
        return value

    return None