import os
from typing import TypeAlias, cast
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv
load_dotenv()

BASE = "https://api.devin.ai/v3"

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


class DevinAPIError(RuntimeError):
    pass


def _headers(content_type: bool = False) -> dict[str, str]:
    api_key = os.environ.get("DEVIN_API_KEY")
    if not api_key:
        raise DevinAPIError("Missing DEVIN_API_KEY")
    headers = {"Authorization": f"Bearer {api_key}"}
    if content_type:
        headers["Content-Type"] = "application/json"
    return headers


def _org_id() -> str:
    org_id = os.environ.get("DEVIN_ORG_ID")
    if not org_id:
        raise DevinAPIError("Missing DEVIN_ORG_ID")
    return org_id


def _json(response: requests.Response) -> JsonObject:
    return cast(JsonObject, response.json())


def verify_credentials() -> JsonObject:
    response = requests.get(
        f"{BASE}/self",
        headers=_headers(),
        timeout=30,
    )
    if response.status_code >= 400:
        raise DevinAPIError(f"Devin auth failed {response.status_code}: {response.text}")
    return _json(response)


def create_session(
    prompt: str,
    tags: list[str] | None = None,
) -> JsonObject:
    """
    Returns:
    {
      "session_id": "devin-abc123",
      "url": "https://app.devin.ai/sessions/devin-abc123",
      "status": "running"
    }
    """

    body: JsonObject = {"prompt": prompt}

    # Only include tags if the API accepts them in your environment.
    # If Devin rejects tags, remove this block.
    if tags:
        body["tags"] = tags
    response = requests.post(
        f"{BASE}/organizations/{_org_id()}/sessions",
        headers=_headers(content_type=True),
        json=body,
        timeout=30,
    )
    if response.status_code >= 400:
        raise DevinAPIError(
            f"Devin create_session failed {response.status_code}: {response.text}"
        )
    return _json(response)


def send_session_message(session_id: str, message: str) -> JsonObject:
    """
    Sends a follow-up message to an existing Devin session.
    Devin auto-resumes the session if it is suspended/awaiting instructions.
    """

    response = requests.post(
        f"{BASE}/organizations/{_org_id()}/sessions/{session_id}/messages",
        headers=_headers(content_type=True),
        json={"message": message},
        timeout=30,
    )
    if response.status_code >= 400:
        raise DevinAPIError(
            f"Devin send_session_message failed {response.status_code}: {response.text}"
        )
    return _json(response)


def list_sessions(session_ids: list[str]) -> list[JsonObject]:
    if not session_ids:
        return []

    response = requests.get(
        f"{BASE}/organizations/{_org_id()}/sessions",
        headers=_headers(),
        params={"session_ids": ",".join(session_ids)},
        timeout=30,
    )
    if response.status_code >= 400:
        raise DevinAPIError(
            f"Devin list_sessions failed {response.status_code}: {response.text}"
        )

    payload = response.json()
    if isinstance(payload, dict):
        sessions = payload.get("sessions") or payload.get("data") or []
        if isinstance(sessions, list):
            return [cast(JsonObject, session) for session in sessions if isinstance(session, dict)]
        if all(isinstance(value, (str, int, float, bool, type(None), dict, list)) for value in payload.values()):
            return [cast(JsonObject, payload)]

    if isinstance(payload, list):
        return [cast(JsonObject, session) for session in payload if isinstance(session, dict)]

    return []


def get_session(session_id: str) -> JsonObject:
    response = requests.get(
        f"{BASE}/organizations/{_org_id()}/sessions/{session_id}",
        headers=_headers(),
        timeout=30,
    )
    if response.status_code >= 400:
        raise DevinAPIError(
            f"Devin get_session failed {response.status_code}: {response.text}"
        )
    return _json(response)


def archive_session(session_id: str) -> JsonObject:
    response = requests.post(
        f"{BASE}/organizations/{_org_id()}/sessions/{session_id}/archive",
        headers=_headers(),
        timeout=30,
    )
    if response.status_code >= 400:
        raise DevinAPIError(
            f"Devin archive_session failed {response.status_code}: {response.text}"
        )
    return _json(response)


def get_session_snapshot(session_id: str) -> JsonObject:
    sessions = list_sessions([session_id])

    if sessions:
        return sessions[0]

    return get_session(session_id)


def get_session_messages(session_id: str) -> JsonObject:
    response = requests.get(
        f"{BASE}/organizations/{_org_id()}/sessions/{session_id}/messages",
        headers=_headers(),
        timeout=30,
    )
    if response.status_code >= 400:
        raise DevinAPIError(
            f"Devin get_session_messages failed {response.status_code}: {response.text}"
        )
    return _json(response)


def request_devin_review(pr_url: str) -> JsonObject:
    response = requests.post(
        f"{BASE}/organizations/{_org_id()}/pr-reviews",
        headers=_headers(content_type=True),
        json={"pr_url": pr_url},
        timeout=30,
    )
    if response.status_code >= 400:
        raise DevinAPIError(
            f"Devin review request failed {response.status_code}: {response.text}"
        )
    return _json(response)


def get_pr_review(pr_url: str, commit_sha: str | None = None) -> JsonObject:
    params: dict[str, str] = {"pr_url": pr_url}
    if commit_sha:
        params["commit_sha"] = commit_sha

    response = requests.get(
        f"{BASE}/organizations/{_org_id()}/pr-reviews",
        headers=_headers(),
        params=params,
        timeout=30,
    )
    if response.status_code >= 400:
        raise DevinAPIError(
            f"Devin review poll failed {response.status_code}: {response.text}"
        )
    return _json(response)


def parse_github_pr_url(pr_url: str) -> tuple[str, str, int]:
    parsed = urlparse(pr_url)
    parts = [part for part in parsed.path.split("/") if part]

    if len(parts) < 4 or parts[2] != "pull":
        raise DevinAPIError(f"Invalid GitHub PR URL: {pr_url}")

    return parts[0], parts[1], int(parts[3])


def is_terminal_status(status: str) -> bool:
    return status in {"exit", "error", "suspended"}


def is_success_status(status: str) -> bool:
    return status == "exit"