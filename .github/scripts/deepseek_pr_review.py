#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any

GITHUB_API = "https://api.github.com"
DEEPSEEK_API = "https://api.deepseek.com/chat/completions"
COMMENT_MARKER = "<!-- deepseek-pr-review -->"

MAX_FILES = 100
MAX_FILE_PATCH_CHARS = 40_000
MAX_TOTAL_DIFF_CHARS = 180_000
MAX_ISSUES = 20

SYSTEM_PROMPT = """You are a senior software engineer reviewing a GitHub pull request.

Prioritize:
1. correctness bugs and regressions
2. security or reliability risks
3. hidden coupling and surprising side effects
4. unnecessary abstractions or dependencies
5. duplicated logic and avoidable cognitive load
6. simpler implementations when the current design is materially harder to understand

Do not:
- nitpick formatting or naming
- request abstractions merely for future extensibility
- restate the diff
- invent problems without concrete evidence
- report low-confidence concerns

Only report issues that are actionable and likely to matter.

Return JSON with exactly this shape:
{
  "summary": "short overall assessment",
  "issues": [
    {
      "path": "relative/file/path",
      "line": 123,
      "severity": "critical|high|medium|low",
      "title": "short title",
      "body": "why this matters and the smallest reasonable fix",
      "confidence": 0.0
    }
  ]
}

For line, use the new-file line number when the issue is tied to a changed line; otherwise use null.
If there are no meaningful issues, return an empty issues list.
"""


def request_json(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
) -> Any:
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "deepseek-pr-review",
            **(headers or {}),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as response:
            payload = response.read().decode("utf-8")
            return json.loads(payload) if payload else None
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {url} failed: HTTP {exc.code}: {detail}") from exc


def github_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def get_pr(repo: str, pr_number: int, token: str) -> dict[str, Any]:
    return request_json(
        f"{GITHUB_API}/repos/{repo}/pulls/{pr_number}",
        headers=github_headers(token),
    )


def get_pr_files(repo: str, pr_number: int, token: str) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    page = 1
    while len(files) < MAX_FILES:
        batch = request_json(
            f"{GITHUB_API}/repos/{repo}/pulls/{pr_number}/files?per_page=100&page={page}",
            headers=github_headers(token),
        )
        if not batch:
            break
        files.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return files[:MAX_FILES]


def is_generated_or_low_signal(path: str) -> bool:
    name = path.rsplit("/", 1)[-1].lower()
    suffixes = (
        ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf",
        ".zip", ".tar", ".gz", ".7z", ".woff", ".woff2", ".ttf",
        ".lock",
    )
    return (
        name in {"package-lock.json", "pnpm-lock.yaml", "yarn.lock", "cargo.lock"}
        or path.lower().endswith(suffixes)
    )


def build_diff_context(files: list[dict[str, Any]]) -> tuple[str, int]:
    sections: list[str] = []
    used = 0
    omitted = 0

    for file in files:
        path = str(file.get("filename", ""))
        patch = file.get("patch")
        header = (
            f"\n### {path}\n"
            f"status={file.get('status')} additions={file.get('additions', 0)} "
            f"deletions={file.get('deletions', 0)}\n"
        )

        if not patch or is_generated_or_low_signal(path):
            sections.append(header + "[patch omitted: binary/generated/lockfile or unavailable]\n")
            omitted += 1
            continue

        clipped = str(patch)[:MAX_FILE_PATCH_CHARS]
        section = header + "```diff\n" + clipped + "\n```\n"
        if used + len(section) > MAX_TOTAL_DIFF_CHARS:
            omitted += 1
            continue
        sections.append(section)
        used += len(section)

    return "".join(sections), omitted


def call_deepseek(api_key: str, model: str, prompt: str) -> dict[str, Any]:
    result = request_json(
        DEEPSEEK_API,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        body={
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "response_format": {"type": "json_object"},
            "reasoning_effort": "high",
            "max_tokens": 8_000,
        },
    )
    try:
        content = result["choices"][0]["message"]["content"]
        review = json.loads(content)
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Unexpected DeepSeek response: {result}") from exc

    if not isinstance(review, dict):
        raise RuntimeError("DeepSeek review was not a JSON object.")
    if not isinstance(review.get("issues", []), list):
        raise RuntimeError("DeepSeek review 'issues' must be a list.")
    return review


def format_comment(review: dict[str, Any], model: str, omitted: int) -> str:
    summary = str(review.get("summary", "")).strip() or "Review completed."
    raw_issues = review.get("issues", [])[:MAX_ISSUES]
    issues = [
        issue
        for issue in raw_issues
        if isinstance(issue, dict)
        and float(issue.get("confidence", 0.0) or 0.0) >= 0.75
    ]

    lines = [
        COMMENT_MARKER,
        "## DeepSeek PR review",
        "",
        summary,
        "",
    ]

    if issues:
        lines.extend(["### Findings", ""])
        for issue in issues:
            path = str(issue.get("path", "unknown"))
            line = issue.get("line")
            location = f"`{path}:{line}`" if isinstance(line, int) else f"`{path}`"
            severity = str(issue.get("severity", "medium")).upper()
            title = str(issue.get("title", "Issue")).strip()
            body = str(issue.get("body", "")).strip()
            confidence = float(issue.get("confidence", 0.0) or 0.0)
            lines.extend([
                f"- **[{severity}] {title}** — {location}  ",
                f"  {body}  ",
                f"  _Confidence: {confidence:.0%}_",
                "",
            ])
    else:
        lines.extend([
            "### Findings",
            "",
            "No high-confidence issues found.",
            "",
        ])

    if omitted:
        lines.extend([
            f"_Note: {omitted} file patch(es) were omitted because they were binary/generated/lockfiles, unavailable, or exceeded the review size limit._",
            "",
        ])

    lines.extend([
        f"_Model: `{model}` · This review is advisory and should not replace tests or human review._",
    ])
    return "\n".join(lines)


def upsert_comment(repo: str, pr_number: int, token: str, body: str) -> None:
    headers = github_headers(token)
    comments = request_json(
        f"{GITHUB_API}/repos/{repo}/issues/{pr_number}/comments?per_page=100",
        headers=headers,
    )
    existing = next(
        (
            comment
            for comment in comments
            if COMMENT_MARKER in str(comment.get("body", ""))
            and comment.get("user", {}).get("login") == "github-actions[bot]"
        ),
        None,
    )

    if existing:
        request_json(
            f"{GITHUB_API}/repos/{repo}/issues/comments/{existing['id']}",
            method="PATCH",
            headers={**headers, "Content-Type": "application/json"},
            body={"body": body},
        )
    else:
        request_json(
            f"{GITHUB_API}/repos/{repo}/issues/{pr_number}/comments",
            method="POST",
            headers={**headers, "Content-Type": "application/json"},
            body={"body": body},
        )


def main() -> int:
    repo = os.environ["GITHUB_REPOSITORY"]
    pr_number = int(os.environ["PR_NUMBER"])
    github_token = os.environ["GITHUB_TOKEN"]
    deepseek_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    model = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-pro").strip()

    if not deepseek_key:
        print(
            "DEEPSEEK_API_KEY is not configured. Add it as a GitHub Actions "
            "repository secret before running this workflow.",
            file=sys.stderr,
        )
        return 2

    pr = get_pr(repo, pr_number, github_token)
    files = get_pr_files(repo, pr_number, github_token)
    diff_context, omitted = build_diff_context(files)

    prompt = f"""Review pull request #{pr_number} in {repo}.

Title: {pr.get('title', '')}
Description:
{pr.get('body') or '(none)'}

Base: {pr.get('base', {}).get('ref', '')}
Head: {pr.get('head', {}).get('ref', '')}

Changed files:
{diff_context or '(no reviewable textual patch available)'}

Concentrate on defects introduced by this PR. Prefer a small number of important findings over a long list.
"""

    review = call_deepseek(deepseek_key, model, prompt)
    comment = format_comment(review, model, omitted)
    upsert_comment(repo, pr_number, github_token, comment)
    print(f"Reviewed PR #{pr_number} with {model}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
