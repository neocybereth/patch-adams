import os
from datetime import datetime, timezone
from typing import Protocol, TypeAlias, cast

from app.db import get_supabase

JsonValue: TypeAlias = (
    str
    | int
    | float
    | bool
    | None
    | dict[str, "JsonValue"]
    | list["JsonValue"]
)
JsonObject: TypeAlias = dict[str, JsonValue]
Run: TypeAlias = dict[str, JsonValue]
RunEvent: TypeAlias = dict[str, JsonValue]

class QueryResult(Protocol):
    data: list[Run]


class QueryBuilder(Protocol):
    def insert(self, value: JsonObject) -> "QueryBuilder": ...
    def update(self, value: JsonObject) -> "QueryBuilder": ...
    def select(self, value: str) -> "QueryBuilder": ...
    def order(self, column: str, desc: bool = False) -> "QueryBuilder": ...
    def limit(self, count: int) -> "QueryBuilder": ...
    def in_(self, column: str, values: list[str] | list[int]) -> "QueryBuilder": ...
    def eq(self, column: str, value: JsonValue) -> "QueryBuilder": ...
    def execute(self) -> QueryResult: ...


class SupabaseClient(Protocol):
    def table(self, name: str) -> QueryBuilder: ...


supabase: SupabaseClient | None = None

ACTIVE_STATUSES = {
    "received",
    "triage_started",
    "triage_completed",
    "remediation_started",
    "pr_created",
    "review_started",
    "review_completed",
}

TERMINAL_STATUSES = {
    "report_ready",
    "verified",
    "failed",
    "needs_human",
    "ignored",
}

REQUIRED_ENVIRONMENT = (
    "SUPABASE_URL",
    "SUPABASE_SERVICE_ROLE_KEY",
    "DEVIN_API_KEY",
    "DEVIN_ORG_ID",
)

REQUIRED_RUN_COLUMNS = (
    "id",
    "status",
    "summary",
    "pr_url",
    "review_session_id",
    "review_session_url",
    "raw_review_output",
)


def _supabase() -> SupabaseClient:
    global supabase

    if supabase is None:
        supabase = cast(SupabaseClient, get_supabase())

    return supabase


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def validate_setup() -> JsonObject:
    missing = [name for name in REQUIRED_ENVIRONMENT if not os.environ.get(name)]
    table_checks: JsonObject = {}
    column_checks: JsonObject = {}

    for table_name in ("runs", "run_events"):
        try:
            _supabase().table(table_name).select("*").limit(1).execute()
            table_checks[table_name] = "ok"
        except Exception as error:
            table_checks[table_name] = f"failed: {error}"

    try:
        _supabase().table("runs").select(",".join(REQUIRED_RUN_COLUMNS)).limit(1).execute()
        column_checks["runs"] = "ok"
    except Exception as error:
        column_checks["runs"] = f"failed: {error}"

    ok = (
        not missing
        and all(value == "ok" for value in table_checks.values())
        and all(value == "ok" for value in column_checks.values())
    )

    return {
        "ok": ok,
        "missing_environment": missing,
        "tables": table_checks,
        "columns": column_checks,
    }


def create_run(
    issue_number: int,
    issue_url: str,
    issue_title: str,
    metadata: JsonObject | None = None,
) -> Run:
    result = (
        _supabase().table("runs")
        .insert({
            "issue_number": issue_number,
            "issue_url": issue_url,
            "issue_title": issue_title,
            "status": "received",
        })
        .execute()
    )

    run = result.data[0]
    run_id = str(run["id"])

    log_event(
        run_id=run_id,
        event_type="received",
        message=f"Received issue #{issue_number}: {issue_title}",
        metadata={
            "issue_url": issue_url,
            **(metadata or {}),
        },
    )

    return run


def create_or_reuse_issue_run(
    issue_number: int,
    issue_url: str,
    issue_title: str,
    metadata: JsonObject | None = None,
) -> tuple[Run, bool]:
    latest = get_latest_run_for_issue(issue_number)

    if latest and latest.get("status") != "failed":
        log_event(
            run_id=str(latest["id"]),
            event_type="duplicate_trigger",
            message=f"Ignored duplicate devin trigger for issue #{issue_number}",
            metadata={
                "issue_url": issue_url,
                **(metadata or {}),
            },
        )
        return latest, False

    return create_run(
        issue_number=issue_number,
        issue_url=issue_url,
        issue_title=issue_title,
        metadata=metadata,
    ), True


def get_latest_run_for_issue(issue_number: int) -> Run | None:
    result = (
        _supabase().table("runs")
        .select("*")
        .eq("issue_number", issue_number)
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )

    if not result.data:
        return None

    return result.data[0]


def get_run(run_id: str) -> Run | None:
    result = (
        _supabase().table("runs")
        .select("*")
        .eq("id", run_id)
        .limit(1)
        .execute()
    )

    if not result.data:
        return None

    return result.data[0]


def get_runs(limit: int = 50) -> list[Run]:
    result = (
        _supabase().table("runs")
        .select("*")
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )

    return result.data or []


def get_active_runs() -> list[Run]:
    result = (
        _supabase().table("runs")
        .select("*")
        .in_("status", sorted(ACTIVE_STATUSES))
        .order("created_at", desc=False)
        .execute()
    )

    return result.data or []


def get_events_for_runs(run_ids: list[str]) -> dict[str, list[RunEvent]]:
    if not run_ids:
        return {}

    result = (
        _supabase().table("run_events")
        .select("*")
        .in_("run_id", run_ids)
        .order("created_at", desc=False)
        .execute()
    )

    events_by_run = {run_id: [] for run_id in run_ids}

    for event in result.data or []:
        run_id = str(event.get("run_id", ""))
        if run_id in events_by_run:
            events_by_run[run_id].append(cast(RunEvent, event))

    return events_by_run


def update_run(run_id: str, fields: JsonObject) -> Run:
    result = (
        _supabase().table("runs")
        .update(fields)
        .eq("id", run_id)
        .select("*")
        .execute()
    )

    return result.data[0]


def transition_run(
    run_id: str,
    expected_status: str,
    fields: JsonObject,
    event_type: str,
    message: str,
    metadata: JsonObject | None = None,
) -> Run | None:
    result = (
        _supabase().table("runs")
        .update(fields)
        .eq("id", run_id)
        .eq("status", expected_status)
        .select("*")
        .execute()
    )

    if not result.data:
        return None

    log_event(
        run_id=run_id,
        event_type=event_type,
        message=message,
        metadata=metadata,
    )

    return result.data[0]


def fail_run(run_id: str, error: str) -> None:
    update_run(run_id, {
        "status": "failed",
        "error": error,
        "completed_at": utc_now(),
    })

    log_event(
        run_id=run_id,
        event_type="failed",
        message=error,
    )


def log_event(
    run_id: str,
    event_type: str,
    message: str | None = None,
    metadata: JsonObject | None = None,
) -> None:
    _supabase().table("run_events").insert({
        "run_id": run_id,
        "event_type": event_type,
        "message": message,
        "metadata": metadata or {},
    }).execute()


def terminalize_active_runs_for_issue(issue_number: int, reason: str) -> int:
    latest = get_latest_run_for_issue(issue_number)

    if not latest or str(latest.get("status")) not in ACTIVE_STATUSES:
        return 0

    run_id = str(latest["id"])
    update_run(run_id, {
        "status": "ignored",
        "error": reason,
        "completed_at": utc_now(),
    })
    log_event(
        run_id=run_id,
        event_type="reset_ignored",
        message=reason,
        metadata={"issue_number": issue_number},
    )

    return 1