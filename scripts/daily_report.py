#!/usr/bin/env python3
"""每天早报/晚报：从 Project #6 汇总未完成和当天完成的任务，发钉钉。

环境变量：
  MODE            morning | evening
  GH_TOKEN        NEXFORM_TOKEN（PAT，读 Project 和 issues）
  DING_TOKEN      钉钉自定义机器人 access_token
  MEMBERS_JSON    成员映射（name / dingtalk_userid / dingtalk_mobile）
  DRY_RUN=1       只打印 payload，不发送
"""
import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")
NOW = datetime.now(TZ)
TODAY = NOW.date()
MODE = os.environ.get("MODE", "morning")
SCOPE = os.environ.get("SCOPE", "active")  # active | all
GH_TOKEN = os.environ.get("GH_TOKEN", "")
DING_TOKEN = os.environ.get("DING_TOKEN", "")
MEMBERS_JSON = os.environ.get("MEMBERS_JSON", "{}")
OWNER = os.environ.get("BOARD_OWNER", "nexform-tech")
NUMBER = os.environ.get("BOARD_NUMBER", "6")
DRY_RUN = os.environ.get("DRY_RUN") == "1"
PROJECT_URL = f"https://github.com/orgs/{OWNER}/projects/{NUMBER}"
PRIO = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
STAT = {"In Progress": 0, "In Review": 1, "Blocked": 2, "Ready": 3}


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


def mention(logins):
    ids, mobiles, tokens, missing = [], [], [], []
    seen = set()
    for login in logins:
        if not login or login in seen or login.endswith("[bot]"):
            continue
        seen.add(login)
        info = members.get(login, {})
        userid = info.get("dingtalk_userid")
        mobile = info.get("dingtalk_mobile")
        if userid:
            ids.append(userid)
            tokens.append(f"@{userid}")
        elif mobile:
            mobiles.append(mobile)
            tokens.append(f"@{mobile}")
        else:
            missing.append(login)
            tokens.append(f"@{login}")
    return ids, mobiles, tokens, missing


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


def item_line(item):
    content = item.get("content") or {}
    repo = content.get("repository", "")
    short = repo.split("/")[-1] if repo else ""
    iteration = (item.get("iteration") or {}).get("title") or "未排期"
    window = iteration_window(item)
    overdue = "，逾期" if window and window[1] < TODAY else ""
    return (
        f"[{item.get('priority') or 'P2'}] "
        f"{short}#{content.get('number')} {content.get('title', '')}"
        f"（{item.get('status') or ''}，{iteration}{overdue}）"
    )


def morning():
    groups = {}
    unassigned = []
    for item in project_items():
        content = item.get("content") or {}
        if content.get("type") != "Issue":
            continue
        if (item.get("status") or "") == "Done":
            continue
        if not in_scope(item):
            continue
        assignees = item.get("assignees") or []
        if not assignees:
            unassigned.append(item)
            continue
        for login in assignees:
            groups.setdefault(login, []).append(item)

    for login in groups:
        groups[login].sort(key=lambda it: (
            PRIO.get(it.get("priority") or "P2", 9),
            STAT.get(it.get("status") or "", 9),
            (it.get("content") or {}).get("repository", ""),
            (it.get("content") or {}).get("number", 0),
        ))

    lines = ["[NexForm] 早安，未完成任务", f"项目看板：{PROJECT_URL}", ""]
    total = 0
    for login, items in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        total += len(items)
        lines.append(f"{real(login)}：{len(items)} 项")
        for item in items[:3]:
            lines.append(f"- {item_line(item)}")
        if len(items) > 3:
            lines.append(f"- …还有 {len(items) - 3} 项")
        lines.append("")

    if unassigned:
        lines.append(f"未分配：{len(unassigned)} 项")
        for item in unassigned[:5]:
            lines.append(f"- {item_line(item)}")
        if len(unassigned) > 5:
            lines.append(f"- …还有 {len(unassigned) - 5} 项")
        lines.append("")

    if total == 0 and not unassigned:
        lines.append("今天没有未完成任务。")

    ids, mobiles, tokens, missing = mention(list(groups.keys()))
    return lines, ids, mobiles, tokens, missing


def evening():
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
            completed.append(issue)

    groups = {}
    for issue in completed:
        for assignee in issue.get("assignees") or []:
            login = assignee.get("login")
            if login:
                groups.setdefault(login, []).append(issue)

    lines = [
        "[NexForm] 今日完成",
        f"今日共完成 {len(completed)} 项。",
        f"项目看板：{PROJECT_URL}",
        "",
    ]
    for login, issues in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        lines.append(f"{real(login)}：{len(issues)} 项")
        for issue in issues[:5]:
            repo = (issue.get("repository") or {}).get("nameWithOwner", "")
            short = repo.split("/")[-1] if repo else ""
            lines.append(f"- {short}#{issue.get('number')} {issue.get('title', '')}")
        if len(issues) > 5:
            lines.append(f"- …还有 {len(issues) - 5} 项")
        lines.append("")

    if not completed:
        lines.append("今天没有完成记录。")

    ids, mobiles, tokens, missing = mention(list(groups.keys()))
    return lines, ids, mobiles, tokens, missing


lines, ids, mobiles, tokens, missing = morning() if MODE == "morning" else evening()
if missing:
    print(f"缺少钉钉联系人: {','.join(missing)}", file=sys.stderr)

body = "\n".join(lines).rstrip()
mention_text = " ".join(tokens)
if mention_text:
    body = body[: max(0, 5000 - len(mention_text) - 2)]
    body += "\n\n" + mention_text
else:
    body = body[:5000]

payload = {
    "msgtype": "text",
    "text": {"content": body},
    "at": {"atUserIds": ids, "atMobiles": mobiles, "isAtAll": False},
}

if DRY_RUN:
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    sys.exit(0)

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
    fail(f"DingTalk request failed: {type(exc).__name__}")
if data.get("errcode") != 0:
    fail(f"DingTalk error: {data}")
print("sent")
