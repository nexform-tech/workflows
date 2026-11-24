#!/usr/bin/env python3
"""GitHub event -> DingTalk 自定义机器人，带 login -> 真实姓名映射。

在 workflow 里通过环境变量传入：
  DING_TOKEN             自定义机器人 access_token
  MEMBERS_JSON           {"login": {"name": "真实姓名"}}
  GITHUB_EVENT_NAME      由 GitHub 自动提供
  GITHUB_EVENT_PATH      由 GitHub 自动提供
  DRY_RUN=1              只打印消息，不发送（本地测试用）
"""
import json
import os
import sys
import urllib.parse
import urllib.request

DING_TOKEN = os.environ.get("DING_TOKEN", "")
MEMBERS_JSON = os.environ.get("MEMBERS_JSON", "{}")
EVENT_NAME = os.environ.get("GITHUB_EVENT_NAME", "")
EVENT_PATH = os.environ.get("GITHUB_EVENT_PATH", "")
DRY_RUN = os.environ.get("DRY_RUN") == "1"


def fail(message):
    print(message, file=sys.stderr)
    sys.exit(1)


if not DING_TOKEN:
    fail("DING_TALK_TOKEN secret is missing")
if not EVENT_PATH:
    fail("GITHUB_EVENT_PATH is missing")

try:
    with open(EVENT_PATH, encoding="utf-8") as fh:
        event = json.load(fh)
except Exception as exc:
    fail(f"cannot read event: {type(exc).__name__}")

try:
    members = json.loads(MEMBERS_JSON or "{}")
except Exception:
    fail("MEMBERS_JSON is not valid JSON")


def real(login):
    if not login:
        return "未知用户"
    name = members.get(login, {}).get("name")
    return f"{name}（{login}）" if name else login


def cut(text, limit=200):
    text = text or ""
    return text[:limit] + ("…" if len(text) > limit else "")


repo = (event.get("repository") or {}).get("full_name", "")
default_branch = (event.get("repository") or {}).get("default_branch", "main")


def build():
    if EVENT_NAME == "issues":
        issue = event.get("issue") or {}
        action = event.get("action", "")
        actor = real((event.get("sender") or {}).get("login"))
        author = real((issue.get("user") or {}).get("login"))
        people = f"操作人：{actor}" if author == actor else f"作者：{author}\n操作人：{actor}"
        return f"Issue {action}", (
            f"### 📌 Issue {action}\n"
            f"**#{issue.get('number')} {cut(issue.get('title'))}**\n"
            f"仓库：{repo}\n{people}\n状态：{issue.get('state', '')}\n"
            f"[查看]({issue.get('html_url', '')})"
        )

    if EVENT_NAME == "issue_comment":
        issue = event.get("issue") or {}
        if issue.get("pull_request"):
            return None
        comment = event.get("comment") or {}
        author = real((comment.get("user") or {}).get("login"))
        return "Issue 新评论", (
            f"### 💬 Issue 新评论\n"
            f"**#{issue.get('number')} {cut(issue.get('title'))}**\n"
            f"仓库：{repo}\n评论人：{author}\n"
            f"[查看]({comment.get('html_url', '')})"
        )

    if EVENT_NAME == "pull_request":
        action = event.get("action", "")
        if action not in {"opened", "closed", "reopened", "ready_for_review"}:
            return None
        pr = event.get("pull_request") or {}
        actor = real((event.get("sender") or {}).get("login"))
        author = real((pr.get("user") or {}).get("login"))
        people = f"操作人：{actor}" if author == actor else f"作者：{author}\n操作人：{actor}"
        return f"PR {action}", (
            f"### 🔀 PR {action}\n"
            f"**#{pr.get('number')} {cut(pr.get('title'))}**\n"
            f"仓库：{repo}\n{people}\n"
            f"[查看]({pr.get('html_url', '')})"
        )

    if EVENT_NAME == "push":
        ref = event.get("ref", "")
        if ref != f"refs/heads/{default_branch}":
            return None
        actor = real((event.get("sender") or {}).get("login"))
        commits = event.get("commits") or []
        return "Push", (
            f"### 🚀 Push\n"
            f"仓库：{repo}\n分支：{ref.replace('refs/heads/', '')}\n"
            f"推送人：{actor}\n提交数：{len(commits)}\n"
            f"[查看]({event.get('compare', '')})"
        )

    return None


built = build()
if not built:
    print("ignored")
    sys.exit(0)

title, text = built
text += "\n\n_NexForm GitHub 通知_"

if DRY_RUN:
    print(json.dumps({"title": title, "text": text}, ensure_ascii=False, indent=2))
    sys.exit(0)

payload = json.dumps(
    {"msgtype": "markdown", "markdown": {"title": title, "text": text}},
    ensure_ascii=False,
).encode("utf-8")
token = urllib.parse.quote(DING_TOKEN, safe="")
url = f"https://oapi.dingtalk.com/robot/send?access_token={token}"
request = urllib.request.Request(
    url,
    data=payload,
    headers={"Content-Type": "application/json"},
    method="POST",
)
try:
    with urllib.request.urlopen(request, timeout=20) as response:
        data = json.loads(response.read().decode("utf-8"))
except Exception as exc:
    fail(f"DingTalk request failed: {type(exc).__name__}")
if data.get("errcode") != 0:
    fail(f"DingTalk error: {data}")
print("sent")
