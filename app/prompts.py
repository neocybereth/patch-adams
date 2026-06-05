def triage_prompt(issue_title: str, issue_url: str) -> str:
    return f"""
You are triaging a GitHub issue in my Superset fork.

Issue title:
{issue_title}

Issue URL:
{issue_url}

Do not modify code.

Your job:
1. Inspect the issue.
2. Decide whether it is safe for autonomous remediation.
3. Recommend the smallest safe fix.

Return your final answer as JSON with exactly this shape:

{{
  "decision": "autofix" | "needs_human" | "ignore",
  "risk": "low" | "medium" | "high" | "hard",
  "category": "frontend" | "backend" | "test" | "docs" | "security" | "unknown",
  "ui_change_expected": true | false,
  "reasoning_summary": "Short explanation of your decision",
  "recommended_fix": "Smallest safe next fix"
}}

Decision rules:
- Choose "autofix" only if the issue is narrow, low-risk, and fixable with a small PR.
- Choose "needs_human" if the issue is ambiguous, risky, large, or requires product judgment.
- Choose "ignore" if the issue is invalid, duplicate, or not actionable.
- Use risk "hard" when the issue is likely to require a large run, broad investigation, or human scope approval before Devin should change code.

If your JSON decision is "autofix", stop after returning the JSON and wait.
If I later reply exactly "proceed with the fix", continue in this same session:
1. Make the smallest safe code change.
2. Add or update targeted tests if appropriate.
3. Run the most relevant targeted tests/checks.
4. Open a pull request against my fork.
5. Include the issue URL in the PR description.
6. If this touches UI, test the affected path and attach or link a recording when possible.

After that follow-up work, return your final answer as JSON with exactly this shape:

{{
  "status": "pr_created" | "blocked" | "failed",
  "pr_url": "https://github.com/...",
  "ui_change": true | false,
  "tests_run": ["..."],
  "video_url": "https://...",
  "summary": "Short summary of the fix",
  "blocker": "Only present when blocked or failed"
}}
"""


def remediation_followup_prompt() -> str:
    return "proceed with the fix"