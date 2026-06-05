from app import runs
from app.runs import JsonObject, JsonValue, Run


class FakeResult:
    def __init__(self, data: list[Run]) -> None:
        self.data = data


class FakeQuery:
    def __init__(self, client: "FakeSupabase", table_name: str) -> None:
        self.client = client
        self.table_name = table_name
        self.insert_value: JsonObject | None = None
        self.update_value: JsonObject | None = None
        self.filters: list[tuple[str, JsonValue]] = []
        self.order_column: str | None = None
        self.order_desc = False
        self.limit_count: int | None = None

    def insert(self, value: JsonObject) -> "FakeQuery":
        self.insert_value = value
        return self

    def update(self, value: JsonObject) -> "FakeQuery":
        self.update_value = value
        return self

    def select(self, value: str) -> "FakeQuery":
        return self

    def order(self, column: str, desc: bool = False) -> "FakeQuery":
        self.order_column = column
        self.order_desc = desc
        return self

    def limit(self, count: int) -> "FakeQuery":
        self.limit_count = count
        return self

    def in_(self, column: str, values: list[str] | list[int]) -> "FakeQuery":
        self.filters.append((column, list(values)))
        return self

    def eq(self, column: str, value: JsonValue) -> "FakeQuery":
        self.filters.append((column, value))
        return self

    def execute(self) -> FakeResult:
        if self.insert_value is not None:
            row = {
                "id": f"{self.table_name}-{len(self.client.rows[self.table_name]) + 1}",
                "created_at": f"2026-06-05T00:00:0{len(self.client.rows[self.table_name])}Z",
                **self.insert_value,
            }
            self.client.rows[self.table_name].append(row)
            return FakeResult([row])

        rows = self.apply_filters(self.client.rows[self.table_name])

        if self.update_value is not None:
            updated: list[Run] = []
            for row in rows:
                row.update(self.update_value)
                updated.append(row)
            return FakeResult(updated)

        if self.order_column:
            rows = sorted(
                rows,
                key=lambda row: str(row.get(self.order_column, "")),
                reverse=self.order_desc,
            )

        if self.limit_count is not None:
            rows = rows[: self.limit_count]

        return FakeResult(rows)

    def apply_filters(self, rows: list[Run]) -> list[Run]:
        filtered = rows
        for column, value in self.filters:
            if isinstance(value, list):
                filtered = [row for row in filtered if row.get(column) in value]
            else:
                filtered = [row for row in filtered if row.get(column) == value]
        return filtered


class FakeSupabase:
    def __init__(self) -> None:
        self.rows: dict[str, list[Run]] = {
            "runs": [],
            "run_events": [],
        }

    def table(self, name: str) -> FakeQuery:
        return FakeQuery(self, name)


def test_create_or_reuse_issue_run_ignores_duplicate_non_failed(monkeypatch) -> None:
    fake = FakeSupabase()
    monkeypatch.setattr(runs, "supabase", fake)

    first, first_created = runs.create_or_reuse_issue_run(
        issue_number=123,
        issue_url="https://github.com/apache/superset/issues/123",
        issue_title="Bug",
        metadata={"source": "github_webhook"},
    )
    second, second_created = runs.create_or_reuse_issue_run(
        issue_number=123,
        issue_url="https://github.com/apache/superset/issues/123",
        issue_title="Bug",
        metadata={"source": "github_webhook"},
    )

    assert first_created is True
    assert second_created is False
    assert first["id"] == second["id"]
    assert len(fake.rows["runs"]) == 1
    assert fake.rows["run_events"][-1]["event_type"] == "duplicate_trigger"


def test_create_or_reuse_issue_run_allows_retry_after_failed(monkeypatch) -> None:
    fake = FakeSupabase()
    monkeypatch.setattr(runs, "supabase", fake)

    first, first_created = runs.create_or_reuse_issue_run(
        issue_number=123,
        issue_url="https://github.com/apache/superset/issues/123",
        issue_title="Bug",
    )
    runs.update_run(str(first["id"]), {"status": "failed"})
    second, second_created = runs.create_or_reuse_issue_run(
        issue_number=123,
        issue_url="https://github.com/apache/superset/issues/123",
        issue_title="Bug",
    )

    assert first_created is True
    assert second_created is True
    assert first["id"] != second["id"]
    assert len(fake.rows["runs"]) == 2


def test_validate_setup_checks_required_run_columns(monkeypatch) -> None:
    fake = FakeSupabase()
    monkeypatch.setattr(runs, "supabase", fake)
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-role")
    monkeypatch.setenv("DEVIN_API_KEY", "devin-key")
    monkeypatch.setenv("DEVIN_ORG_ID", "org-1")

    result = runs.validate_setup()

    assert result["columns"]["runs"] == "ok"


def test_active_runs_exclude_terminal_statuses(monkeypatch) -> None:
    fake = FakeSupabase()
    monkeypatch.setattr(runs, "supabase", fake)
    fake.rows["runs"].extend([
        {"id": "1", "issue_number": 1, "status": "received"},
        {"id": "2", "issue_number": 2, "status": "report_ready"},
        {"id": "3", "issue_number": 3, "status": "failed"},
        {"id": "4", "issue_number": 4, "status": "review_started"},
    ])

    active = runs.get_active_runs()

    assert [row["id"] for row in active] == ["1", "4"]
