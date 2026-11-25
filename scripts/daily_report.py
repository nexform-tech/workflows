#!/usr/bin/env python3
"""每天早报/晚报：从 Project #6 汇总任务，按人分开发钉钉。

每份报告是一条独立的 markdown 卡片：早报只发给有未完成任务的人，
晚报只发给当天有完成项的人。未分配任务、当天无内容的人都不发送。

钉钉 markdown 的 @ 在手机端不高亮、不可点，所以这里不再发集中 @ 的
text 消息；每条报告直接发到群里。

环境变量：
  MODE            morning | evening
  SCOPE           active | all（早报范围，默认 active）
  SEND_INTERVAL   两条消息之间的间隔秒数（默认 3.2，钉钉限制 20 条/分钟）
  GH_TOKEN        NEXFORM_TOKEN（PAT，读 Project 和 issues）
  DING_TOKEN      钉钉自定义机器人 access_token
  MEMBERS_JSON    成员映射（name / dingtalk_userid / dingtalk_mobile）
  DRY_RUN=1       只打印 payload，不发送
"""
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")
NOW = datetime.now(TZ)
TODAY = NOW.date()
MODE = os.environ.get("MODE", "morning")
SCOPE = os.environ.get("SCOPE", "active")
GH_TOKEN = os.environ.get("GH_TOKEN", "")
DING_TOKEN = os.environ.get("DING_TOKEN", "")
MEMBERS_JSON = os.environ.get("MEMBERS_JSON", "{}")
OWNER = os.environ.get("BOARD_OWNER", "nexform-tech")
NUMBER = os.environ.get("BOARD_NUMBER", "6")
DRY_RUN = os.environ.get("DRY_RUN") == "1"
PROJECT_URL = f"https://github.com/orgs/{OWNER}/projects/{NUMBER}"
PRIO = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
STAT = {"In Progress": 0, "In Review": 1, "Blocked": 2, "Ready": 3}
# 钉钉主动消息按序列化后的 {title,text} JSON 计 15000 UTF-8 字节上限，
# 这里给标题、JSON 结构和转义留出余量。
MAX_MD_BYTES = 14000

try:
    SEND_INTERVAL = max(0.0, float(os.environ.get("SEND_INTERVAL", "3.2")))
except ValueError:
    SEND_INTERVAL = 3.2


def fail(message):
    print(message, file=sys.stderr)
    sys.exit(1)


if not GH_TOKEN:
    fail("NEXFORM_TOKEN/GH_TOKEN is missing")
if not DING_TOKEN:
    fail("DING_TALK_TOKEN is missing")

try:
    raw = json.loads(MEMBERS_JSON or "{}")
except Exception:
    fail("MEMBERS_JSON is not valid JSON")
members = raw.get("members", raw)


def real(login):
    if not login:
        return "未知用户"
    name = members.get(login, {}).get("name")
    return f"{name}（{login}）" if name else login


def md_escape(text):
    return (text or "").replace("[", "\\[").replace("]", "\\]")


def cut(text, limit=80):
    text = text or ""
    return text[:limit] + ("…" if len(text) > limit else "")


def gh_json(*args):
    env = dict(os.environ)
    env["GH_TOKEN"] = GH_TOKEN
    p = subprocess.run(["gh", *args], text=True, capture_output=True, env=env)
    if p.returncode != 0:
        fail(f"gh {' '.join(args)} failed: {p.stderr.strip()[:500]}")
    try:
        return json.loads(p.stdout or "{}")
    except Exception:
        fail(f"gh {' '.join(args)} returned invalid JSON")


def project_items():
    data = gh_json(
        "project", "item-list", NUMBER, "--owner", OWNER,
        "--limit", "1000", "--format", "json",
    )
    return data.get("items", [])


def iteration_window(item):
    iteration = item.get("iteration") or {}
    start = iteration.get("startDate")
    duration = iteration.get("duration")
    if not start or not duration:
        return None
    start_date = date.fromisoformat(start)
    return start_date, start_date + timedelta(days=int(duration) - 1)


def in_scope(item):
    if SCOPE == "all":
        return True
    status = item.get("status") or ""
    window = iteration_window(item)
    current = bool(window and window[0] <= TODAY <= window[1])
    overdue = bool(window and window[1] < TODAY)
    if status in {"In Progress", "In Review", "Blocked"}:
        return True
    if status == "Ready":
        return current or overdue
    return current or overdue


def task_line(item):
    content = item.get("content") or {}
    repo = content.get("repository", "")
    short = repo.split("/")[-1] if repo else ""
    iteration = (item.get("iteration") or {}).get("title") or "未排期"
    window = iteration_window(item)
    overdue = " · ⚠️ 逾期" if window and window[1] < TODAY else ""
    return (
        f"- **{item.get('priority') or 'P2'}** "
        f"[{md_escape(cut(content.get('title', '')))}]({content.get('url', '')})"
        f" · {short}#{content.get('number')} · {item.get('status') or ''}"
        f" · {iteration}{overdue}"
    )


def morning():
    """每人一份早报：有未完成任务的人各返回一条 markdown。"""
    groups = {}
    for item in project_items():
        content = item.get("content") or {}
        if content.get("type") != "Issue":
            continue
        if (item.get("status") or "") == "Done":
            continue
        if not in_scope(item):
            continue
        for login in item.get("assignees") or []:
            groups.setdefault(login, []).append(item)

    reports = []
    for login, items in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        items.sort(key=lambda it: (
            PRIO.get(it.get("priority") or "P2", 9),
            STAT.get(it.get("status") or "", 9),
            (it.get("content") or {}).get("repository", ""),
            (it.get("content") or {}).get("number", 0),
        ))
        lines = [
            f"## 🌅 NexForm 早安 · {TODAY}",
            f"> 👤 {real(login)} · 未完成 {len(items)} 项 · "
            f"[项目看板]({PROJECT_URL})",
            "",
        ]
        for item in items:
            lines.append(task_line(item))
        reports.append((
            login,
            f"NexForm 早安 · {real(login)}",
            "\n".join(lines).rstrip(),
        ))
    return reports


def evening():
    """每人一份晚报：当天有完成项的人各返回一条 markdown。"""
    project_urls = {
        (item.get("content") or {}).get("url")
        for item in project_items()
        if (item.get("content") or {}).get("type") == "Issue"
    }
    since = TODAY.isoformat()
    issues = gh_json(
        "search", "issues", "--owner", OWNER, "--state", "closed",
        "--closed", f">={since}", "--limit", "300",
        "--json", "number,title,url,repository,closedAt,assignees",
    )
    completed = []
    for issue in issues:
        if issue.get("url") not in project_urls:
            continue
        try:
            closed = datetime.fromisoformat(
                issue["closedAt"].replace("Z", "+00:00")
            ).astimezone(TZ)
        except Exception:
            continue
        if closed.date() == TODAY:
            completed.append((issue, closed))

    groups = {}
    for issue, closed in completed:
        for assignee in issue.get("assignees") or []:
            login = assignee.get("login")
            if login:
                groups.setdefault(login, []).append((issue, closed))

    reports = []
    for login, entries in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        entries.sort(key=lambda entry: entry[1])
        lines = [
            f"## 🌙 NexForm 晚报 · {TODAY}",
            f"> 👤 {real(login)} · 今日完成 {len(entries)} 项 · "
            f"[项目看板]({PROJECT_URL})",
            "",
        ]
        for issue, closed in entries:
            repo = (issue.get("repository") or {}).get("nameWithOwner", "")
            short = repo.split("/")[-1] if repo else ""
            lines.append(
                f"- [{md_escape(cut(issue.get('title', '')))}]({issue.get('url', '')})"
                f" · {short}#{issue.get('number')} · {closed.strftime('%H:%M')}"
            )
        reports.append((
            login,
            f"NexForm 晚报 · {real(login)}",
            "\n".join(lines).rstrip(),
        ))
    return reports


def cap_markdown(text):
    raw = text.encode("utf-8")
    if len(raw) <= MAX_MD_BYTES:
        return text
    return raw[:MAX_MD_BYTES].decode("utf-8", "ignore") + (
        f"\n\n…完整内容请看[项目看板]({PROJECT_URL})"
    )


def send(payload, label):
    """发送一条消息；成功返回 True，失败打印原因并返回 False。"""
    if DRY_RUN:
        print(f"=== {label} ===")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return True
    token = urllib.parse.quote(DING_TOKEN, safe="")
    url = f"https://oapi.dingtalk.com/robot/send?access_token={token}"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        print(f"{label} request failed: {type(exc).__name__}", file=sys.stderr)
        return False
    if data.get("errcode") != 0:
        print(f"{label} DingTalk error: {data}", file=sys.stderr)
        return False
    print(f"sent {label}")
    return True


reports = morning() if MODE == "morning" else evening()
if not reports:
    print("nothing to report")
    sys.exit(0)

label = "早报" if MODE == "morning" else "晚报"
failed = []
for index, (login, title, text) in enumerate(reports):
    if index and not DRY_RUN and SEND_INTERVAL > 0:
        time.sleep(SEND_INTERVAL)
    payload = {
        "msgtype": "markdown",
        "markdown": {"title": title, "text": cap_markdown(text)},
    }
    if not send(payload, f"{label} · {real(login)}"):
        failed.append(login)

if failed:
    fail(f"failed to send {len(failed)} report(s): {','.join(failed)}")
