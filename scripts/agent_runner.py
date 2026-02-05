#!/usr/bin/env python3
"""
PermitCard Agent Runner

Reads GitHub Issues labeled "agent", uses OpenAI to propose a small code change set,
applies the changes, pushes a branch, and opens a PR.

Required env vars (provided by GitHub Actions workflow env:):
- OPENAI_API_KEY
- GITHUB_TOKEN
- GITHUB_REPOSITORY (e.g. "Vinnypoker/PermitCard")
"""

from __future__ import annotations

import datetime as dt
import json
import os
import random
import subprocess
import time
from typing import Any, Dict, List, Optional

import requests

# -----------------------------
# Environment / constants
# -----------------------------
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
REPO = os.getenv("GITHUB_REPOSITORY")  # owner/repo
API = "https://api.github.com"

if not OPENAI_API_KEY or not GITHUB_TOKEN or not REPO:
    raise SystemExit("Missing OPENAI_API_KEY, GITHUB_TOKEN, or GITHUB_REPOSITORY")

GH_HEADERS = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
}

OPENAI_HEADERS = {
    "Authorization": f"Bearer {OPENAI_API_KEY}",
    "Content-Type": "application/json",
}

AGENT_LABEL = "agent"
IN_PROGRESS_LABEL = "agent-in-progress"
DONE_LABEL = "agent-done"  # optional: not used automatically


# -----------------------------
# Git helpers
# -----------------------------
def run(cmd: str) -> None:
    subprocess.check_call(cmd, shell=True)


def git_config_identity() -> None:
    run("git config user.name 'permit-agent'")
    run("git config user.email 'permit-agent@users.noreply.github.com'")


# -----------------------------
# GitHub API helpers
# -----------------------------
def gh_get(path: str, params: Optional[dict] = None) -> Any:
    r = requests.get(f"{API}{path}", headers=GH_HEADERS, params=params, timeout=60)
    r.raise_for_status()
    return r.json()


def gh_post(path: str, payload: dict) -> Any:
    r = requests.post(f"{API}{path}", headers=GH_HEADERS, json=payload, timeout=60)
    r.raise_for_status()
    return r.json()


def gh_put(path: str, payload: dict) -> Any:
    r = requests.put(f"{API}{path}", headers=GH_HEADERS, json=payload, timeout=60)
    r.raise_for_status()
    return r.json()


def comment(issue_number: int, body: str) -> None:
    gh_post(f"/repos/{REPO}/issues/{issue_number}/comments", {"body": body})


def add_labels(issue_number: int, labels: List[str]) -> None:
    # POST labels replaces? GitHub accepts {"labels":[...]} to add
    gh_post(f"/repos/{REPO}/issues/{issue_number}/labels", {"labels": labels})


def remove_label(issue_number: int, label: str) -> None:
    # ignore if missing
    try:
        requests.delete(
            f"{API}/repos/{REPO}/issues/{issue_number}/labels/{label}",
            headers=GH_HEADERS,
            timeout=60,
        ).raise_for_status()
    except Exception:
        pass


def open_pr(branch: str, title: str, body: str) -> str:
    pr = gh_post(
        f"/repos/{REPO}/pulls",
        {"title": title, "head": branch, "base": "main", "body": body},
    )
    return pr.get("html_url", "")


def pick_next_issue() -> Optional[dict]:
    """
    Fetch open issues and select the oldest one labeled "agent",
    skipping PRs and issues already in-progress.
    """
    issues = gh_get(f"/repos/{REPO}/issues", params={"state": "open", "per_page": 50})

    for it in issues:
        # Skip PRs
        if "pull_request" in it:
            continue

        labels = [lab.get("name", "") for lab in it.get("labels", [])]
        if AGENT_LABEL in labels and IN_PROGRESS_LABEL not in labels:
            return it

    return None


# -----------------------------
# OpenAI call (with backoff)
# -----------------------------
def openai_json_response(prompt: str) -> Dict[str, Any]:
    """
    Calls OpenAI Responses API and expects a JSON object in output_text.
    Retries on 429 and transient 5xx.
    """
    url = "https://api.openai.com/v1/responses"

    payload = {
        "model": "gpt-5.2",
        "input": [{"role": "user", "content": prompt}],
        "text": {"format": {"type": "json_object"}},
    }

    max_attempts = 8
    for attempt in range(1, max_attempts + 1):
        r = requests.post(url, headers=OPENAI_HEADERS, json=payload, timeout=180)

        # Retry on rate limits or transient server errors
        if r.status_code == 429 or (500 <= r.status_code < 600):
            wait = min(90.0, (2 ** attempt)) + random.uniform(0.0, 2.0)
            print(f"OpenAI HTTP {r.status_code}. Retry {attempt}/{max_attempts} in {wait:.1f}s")
            time.sleep(wait)
            continue

        # Non-retry error
        if r.status_code >= 400:
            try:
                print("OpenAI error body:", r.text[:1000])
            except Exception:
                pass
            r.raise_for_status()

        data = r.json()

        # Extract output_text from Responses payload
        out_text = ""
        for item in data.get("output", []):
            if item.get("type") == "output_text":
                out_text += item.get("text", "")
            for c in item.get("content", []):
                if c.get("type") == "output_text":
                    out_text += c.get("text", "")

        return json.loads(out_text)

    raise RuntimeError("OpenAI request failed after retries (rate limit or server error).")


def build_prompt(issue_title: str, issue_body: str) -> str:
    return f"""
You are a senior full-stack engineer working in a GitHub repo named "{REPO}".

Goal of this repo:
Build a web app (Next.js + FastAPI) that lets a user upload a floor plan image/PDF, add annotations,
enter scope, then generate inspector-visible permit PDF sheets.

Task to implement now (do a SMALL, working increment):
Title: {issue_title}
Details:
{issue_body}

Rules:
- Keep changes minimal and runnable.
- If adding dependencies, update requirements.txt or package.json accordingly.
- Prefer incremental delivery (MVP scaffolding first).
- Output STRICT JSON ONLY with:
  - "branch_name": string (kebab-case, short)
  - "changes": array of objects: {{"path": "...", "content": "..."}}
  - "commit_message": string
  - "pr_title": string
  - "pr_body": string
"""


# -----------------------------
# Main agent logic
# -----------------------------
def apply_changes(changes: List[Dict[str, str]]) -> None:
    for ch in changes:
        path = ch["path"].strip()
        content = ch["content"]

        # Ensure directory exists
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)

        with open(path, "w", encoding="utf-8") as f:
            f.write(content)


def main() -> None:
    issue = pick_next_issue()
    if not issue:
        print("No available issues labeled 'agent' to process.")
        return

    issue_number = issue["number"]
    title = issue["title"]
    body = issue.get("body") or ""

    # Mark in progress to avoid duplicate runs
    add_labels(issue_number, [IN_PROGRESS_LABEL])

    started = dt.datetime.utcnow().isoformat() + "Z"
    comment(issue_number, f"🤖 Agent picked up this issue at {started}.\n\nWorking on it now...")

    try:
        prompt = build_prompt(title, body)
        plan = openai_json_response(prompt)

        branch = plan["branch_name"]
        changes = plan["changes"]
        commit_message = plan["commit_message"]
        pr_title = plan["pr_title"]
        pr_body = plan["pr_body"]

        # Git operations
        git_config_identity()
        run(f"git checkout -b {branch}")

        apply_changes(changes)

        run("git add -A")
        run(f'git commit -m "{commit_message}"')
        run(f"git push -u origin {branch}")

        pr_url = open_pr(branch, pr_title, pr_body)

        comment(
            issue_number,
            f"✅ Opened PR: {pr_url}\n\nIf you want changes, comment on the PR or this issue and I’ll iterate.",
        )

    except Exception as e:
        # Remove in-progress label so it can retry later
        remove_label(issue_number, IN_PROGRESS_LABEL)
        comment(issue_number, f"❌ Agent failed:\n\n```\n{type(e).__name__}: {e}\n```\n\nCheck Actions logs for details.")
        raise


if __name__ == "__main__":
    main()
