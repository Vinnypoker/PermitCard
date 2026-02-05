raise SystemExit("DEBUG: NEW AGENT RUNNER FILE IS EXECUTING")
import os
import json
import subprocess
import datetime
import requests

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
REPO = os.getenv("GITHUB_REPOSITORY")  # e.g. Vinnypoker/PermitCard
API = "https://api.github.com"

if not GITHUB_TOKEN or not OPENAI_API_KEY or not REPO:
    raise SystemExit("Missing GITHUB_TOKEN, OPENAI_API_KEY, or GITHUB_REPOSITORY")

HEADERS = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
}

def gh_get(path, params=None):
    r = requests.get(f"{API}{path}", headers=HEADERS, params=params, timeout=60)
    r.raise_for_status()
    return r.json()

def gh_post(path, payload):
    r = requests.post(f"{API}{path}", headers=HEADERS, json=payload, timeout=60)
    r.raise_for_status()
    return r.json()

def comment(issue_number: int, body: str):
    gh_post(f"/repos/{REPO}/issues/{issue_number}/comments", {"body": body})

def pick_next_issue():
    # Pick the oldest open issue with label "agent"
    issues = gh_get(f"/repos/{REPO}/issues", params={"state": "open", "labels": "agent", "per_page": 20})
    for it in issues:
        if "pull_request" in it:
            continue
        return it
    return None

def open_pr(branch: str, title: str, body: str):
    return gh_post(f"/repos/{REPO}/pulls", {
        "title": title,
        "head": branch,
        "base": "main",
        "body": body
    })

def run(cmd):
    subprocess.check_call(cmd, shell=True)

def openai_plan(issue_title: str, issue_body: str):
    url = "https://api.openai.com/v1/responses"
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    prompt = f"""
You are a senior full-stack developer.
Build the requested feature in small, working increments.
Repo goal: Next.js frontend + FastAPI backend that generates inspector-visible permit PDFs.

Task:
Title: {issue_title}
Details: {issue_body}

Return STRICT JSON with:
- branch_name: short-kebab-case
- changes: array of file edits with {{path, content}}
- commit_message
- pr_title
- pr_body

Keep changes minimal and runnable.
"""
    payload = {
        "model": "gpt-5.2",
        "input": [{"role": "user", "content": prompt}],
        "text": {"format": {"type": "json_object"}}
    }
    r = requests.post(url, headers=headers, json=payload, timeout=180)
    r.raise_for_status()
    data = r.json()

    out_text = ""
    for item in data.get("output", []):
        for c in item.get("content", []):
            if c.get("type") == "output_text":
                out_text += c.get("text", "")
        if item.get("type") == "output_text":
            out_text += item.get("text", "")

    return json.loads(out_text)

def main():
    issue = pick_next_issue()
    if not issue:
        print("No agent issues found.")
        return

    num = issue["number"]
    title = issue["title"]
    body = issue.get("body") or ""

    comment(num, f"🤖 Agent picked up this issue at {datetime.datetime.utcnow().isoformat()}Z")

    plan = openai_plan(title, body)
    branch = plan["branch_name"]

    # git identity
    run("git config user.name 'permit-agent'")
    run("git config user.email 'permit-agent@users.noreply.github.com'")

    # create branch
    run(f"git checkout -b {branch}")

    # write changes
    for ch in plan["changes"]:
        path = ch["path"]
        content = ch["content"]
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)

    run("git add -A")
    run(f"git commit -m \"{plan['commit_message']}\"")
    run(f"git push -u origin {branch}")

    pr = open_pr(branch, plan["pr_title"], plan["pr_body"])
    pr_url = pr.get("html_url", "")
    comment(num, f"✅ Opened PR: {pr_url}\n\nReview it, request changes in comments, or merge.")

if __name__ == "__main__":
    main()
