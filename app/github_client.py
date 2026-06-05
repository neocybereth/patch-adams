import os
from typing import TypeAlias, cast

import requests
from dotenv import load_dotenv

load_dotenv()

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

GITHUB_API_BASE = "https://api.github.com"


class GitHubAPIError(RuntimeError):
    pass


def _headers() -> dict[str, str]:
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise GitHubAPIError("Missing GITHUB_TOKEN")

    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def post_issue_comment(
    repository: str,
    issue_number: int,
    body: str,
) -> JsonObject:
    try:
        response = requests.post(
            f"{GITHUB_API_BASE}/repos/{repository}/issues/{issue_number}/comments",
            headers=_headers(),
            json={"body": body},
            timeout=30,
        )
    except requests.RequestException as error:
        raise GitHubAPIError(f"GitHub comment request failed: {error}") from error

    if response.status_code >= 400:
        raise GitHubAPIError(
            f"GitHub comment failed {response.status_code}: {response.text}"
        )

    return cast(JsonObject, response.json())
