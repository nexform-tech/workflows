#!/usr/bin/env python3
"""每天早报/晚报：汇总全组织工作，按人分开发钉钉。

早报：看板未完成任务 + 全组织待办（我名下 open issue、我开的 open PR、
待我 review 的 PR）。
晚报：全组织今日活动（提交、新建/关闭 issue、新建/合并/关闭 PR、
PR review、issue/PR 评论、发布 release）。

每份报告是一条独立的 markdown 卡片；没有内容的人不发送。所有人都没有
内容时，发一条群消息兜底，确认脚本跑过。钉钉 markdown 的 @ 在手机端
不高亮、不可点，所以这里不再发集中 @ 的 text 消息。

晚报按仓库事件流的 actor 归属（按操作人）：
- 提交 -> 推送人
- 新建 issue / PR -> 操作人
- 关闭 issue -> 关闭人；机器人关闭时回退到负责人/作者
- 合并 / 关闭 PR -> 合并人 / 关闭人；机器人操作时回退到作者
- Review / 评论 -> 评审人 / 评论人
- 发版 -> 触发发版的合并 PR 的合并人；找不到时回退到 release 作者

环境变量：
  MODE            morning | evening
  SCOPE           active | all（早报看板范围，默认 active）
  SEND_INTERVAL   两条消息之间的间隔秒数（默认 3.2，钉钉限制 20 条/分钟）
  GH_TOKEN        NEXFORM_TOKEN（PAT，读 Project、issue、PR、事件流）
  DING_TOKEN      钉钉自定义机器人 access_token
  MEMBERS_JSON    成员映射（name / dingtalk_userid / dingtalk_mobile）
  DRY_RUN=1       只打印 payload，不发送
  FORCE_SEND=1    绕过发送时间窗（手动补发用）

GitHub 的 schedule 可能延迟数小时。脚本只在预期窗口内发送：
早报 08:30-10:30、晚报 18:00-20:30（Asia/Shanghai），超出窗口直接跳过。
workflow_dispatch、DRY_RUN、FORCE_SEND 会绕过这个检查。
"""
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")
NOW = datetime.now(TZ)
TODAY = NOW.date()
SINCE = (TODAY - timedelta(days=1)).isoformat()
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
BOTS = {"github-actions", "semantic-release-bot", "dependabot", "renovate"}
KIND_ORDER = [
    "提交", "合并 PR", "发版", "新建 PR", "关闭 PR",
    "关闭 issue", "新建 issue", "Review", "评论",
]
REVIEW_STATES = {
    "APPROVED": "通过",
    "CHANGES_REQUESTED": "请求修改",
    "COMMENTED": "评论",
    "DISMISSED": "已忽略",
}

try:
    SEND_INTERVAL = max(0.0, float(os.environ.get("SEND_INTERVAL", "3.2")))
except ValueError:
    SEND_INTERVAL = 3.2
FORCE_SEND = os.environ.get("FORCE_SEND") == "1"


def within_window():
    """GitHub schedule 可能延迟数小时，只在预期窗口内发送。"""
    if DRY_RUN or FORCE_SEND:
        return True
    if os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch":
        return True
    minutes = NOW.hour * 60 + NOW.minute
    if MODE == "morning":
        return 8 * 60 + 30 <= minutes <= 10 * 60 + 30
    return 18 * 60 <= minutes <= 20 * 60 + 30


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


def is_bot(login):
    if not login:
        return True
    login = login.lower()
    return login.endswith("[bot]") or login in BOTS


def human(login):
    return login if login and not is_bot(login) else None


def first_human(logins):
    for login in logins:
        if human(login):
            return login
    return None


def to_local(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(TZ)


def on_today(ts):
    try:
        return bool(ts) and to_local(ts).date() == TODAY
    except Exception:
        return False


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


def api_get(path, default=None):
    """非致命 REST 读取：失败时返回 default，不中断整份报告。"""
    env = dict(os.environ)
    env["GH_TOKEN"] = GH_TOKEN
    p = subprocess.run(["gh", "api", path], text=True, capture_output=True, env=env)
    if p.returncode != 0:
        print(f"gh api {path} failed: {p.stderr.strip()[:200]}", file=sys.stderr)
        return default
    try:
        return json.loads(p.stdout or "null")
    except Exception:
        return default


def org_members():
    """当前组织成员 login 集合；取不到时返回空集，报告不过滤。"""
    found = set()
    for page in range(1, 11):
        batch = api_get(
            f"orgs/{OWNER}/members?per_page=100&page={page}", [],
        ) or []
        for user in batch:
            login = login_of(user)
            if login:
                found.add(login.lower())
        if len(batch) < 100:
            break
    return found


ISSUE_FIELDS = (
    "number,title,url,repository,createdAt,closedAt,author,assignees,state"
)
PR_FIELDS = (
    "number,title,url,repository,createdAt,closedAt,author,state,isDraft"
)


def search_issues(*args):
    return gh_json(
        "search", "issues", "--owner", OWNER, "--limit", "1000",
        *args, "--json", ISSUE_FIELDS,
    )


def search_prs(*args):
    return gh_json(
        "search", "prs", "--owner", OWNER, "--limit", "1000",
        *args, "--json", PR_FIELDS,
    )


def search_commits():
    return gh_json(
        "search", "commits", "--owner", OWNER,
        "--committer-date", f">={SINCE}", "--limit", "1000",
        "--json", "sha,url,repository,author,committer,commit,parents",
    )


def repo_short(obj):
    full = (obj or {}).get("nameWithOwner", "")
    return full.split("/")[-1] if full else ""


def login_of(obj):
    return (obj or {}).get("login")


def issue_ref(item):
    return f"{repo_short(item.get('repository'))}#{item.get('number')}"


def item_assignees(item):
    result = []
    for assignee in item.get("assignees") or []:
        login = login_of(assignee) if isinstance(assignee, dict) else assignee
        if human(login):
            result.append(login)
    return result


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


def board_data():
    """返回 (按人分组的未完成任务, 看板上所有 Issue 的 URL 集合)。"""
    groups, all_urls = {}, set()
    for item in project_items():
        content = item.get("content") or {}
        if content.get("type") != "Issue":
            continue
        url = content.get("url")
        if url:
            all_urls.add(url)
        if (item.get("status") or "") == "Done" or not in_scope(item):
            continue
        for login in item_assignees(item):
            groups.setdefault(login, []).append(item)
    for login in groups:
        groups[login].sort(key=lambda it: (
            PRIO.get(it.get("priority") or "P2", 9),
            STAT.get(it.get("status") or "", 9),
            (it.get("content") or {}).get("repository", ""),
            (it.get("content") or {}).get("number", 0),
        ))
    return groups, all_urls


def morning():
    """每人一份早报：看板任务 + 全组织待办。"""
    board, board_urls = board_data()
    current = org_members()

    def allowed(login):
        return not current or (login or "").lower() in current

    board = {
        login: items for login, items in board.items() if allowed(login)
    }
    todos = {}
    seen = {}

    def add(login, kind, title, url, ref, extra=""):
        login = human(login)
        if not login or not url or not allowed(login):
            return
        urls = seen.setdefault(login, set())
        if url in urls:
            return
        urls.add(url)
        todos.setdefault(login, []).append({
            "kind": kind, "title": title, "url": url,
            "ref": ref, "extra": extra,
        })

    for login, items in board.items():
        for item in items:
            url = (item.get("content") or {}).get("url")
            if url:
                seen.setdefault(login, set()).add(url)

    for issue in search_issues("--state", "open"):
        if issue.get("url") in board_urls:
            continue
        for login in item_assignees(issue):
            add(login, "issue", issue.get("title"), issue.get("url"),
                issue_ref(issue))

    for pr in search_prs("--state", "open"):
        add(login_of(pr.get("author")), "pr", pr.get("title"),
            pr.get("url"), issue_ref(pr),
            "draft" if pr.get("isDraft") else "open")

    candidates = set(board) | set(todos) | set(members)
    for login in sorted(candidates):
        if is_bot(login) or not allowed(login):
            continue
        for pr in search_prs(
            "--state", "open", "--review-requested", login,
        ):
            add(login, "review", pr.get("title"), pr.get("url"),
                issue_ref(pr))

    people = set(board) | set(todos)
    reports = []
    for login in sorted(
        people,
        key=lambda l: (-(len(board.get(l, [])) + len(todos.get(l, []))), l),
    ):
        items = board.get(login, [])
        other = todos.get(login, [])
        if not items and not other:
            continue
        lines = [
            f"## 🌅 NexForm 早安 · {TODAY}",
            f"> 👤 {real(login)} · 未完成 {len(items) + len(other)} 项 · "
            f"[项目看板]({PROJECT_URL})",
            "",
        ]
        if items:
            lines.append(f"**看板任务（{len(items)}）**")
            for item in items:
                lines.append(task_line(item))
            lines.append("")
        if other:
            labels = {"issue": "issue", "pr": "PR", "review": "待 review"}
            lines.append(f"**其他待办（{len(other)}）**")
            for entry in sorted(other, key=lambda e: (e["kind"], e["ref"])):
                extra = f" · {entry['extra']}" if entry["extra"] else ""
                lines.append(
                    f"- [{labels.get(entry['kind'], entry['kind'])}] "
                    f"[{md_escape(cut(entry['title']))}]({entry['url']})"
                    f" · {entry['ref']}{extra}"
                )
            lines.append("")
        reports.append((
            login,
            f"NexForm 早安 · {real(login)}",
            "\n".join(lines).rstrip(),
        ))
    return reports


EVENT_WORKERS = 8


def repo_events():
    """并发拉取组织内非归档仓库的最新事件流（最多 3 页/仓库）。"""
    repos = gh_json(
        "repo", "list", OWNER, "--limit", "200",
        "--json", "name,isArchived",
    )
    names = [
        repo.get("name") for repo in repos
        if not repo.get("isArchived") and repo.get("name")
    ]

    def fetch(name):
        events = []
        for page in range(1, 4):
            batch = api_get(
                f"repos/{OWNER}/{name}/events?per_page=100&page={page}", [],
            ) or []
            events.extend(batch)
            if len(batch) < 100:
                break
        return events

    events = []
    with ThreadPoolExecutor(max_workers=EVENT_WORKERS) as pool:
        for batch in pool.map(fetch, names):
            events.extend(batch)
    print(f"repos: {len(names)}, events: {len(events)}", file=sys.stderr)
    return events


def event_repo(event):
    return (event.get("repo") or {}).get("name", "").split("/")[-1]


def event_actor(event):
    return login_of(event.get("actor"))


def event_time(event):
    return event.get("created_at")


def merge_commit(message):
    message = (message or "").lstrip().lower()
    return message.startswith((
        "merge pull request", "merge branch", "merge remote-tracking",
    ))


def evening():
    """每人一份晚报：提交 + issue/PR/review/评论/发版。"""
    events = repo_events()
    activities = {}
    seen_commits = set()
    merged_prs = {}
    pr_cache = {}

    def add(login, kind, title, url, ref, ts, note=""):
        login = human(login)
        if not login or not url:
            return
        activities.setdefault(login, []).append({
            "kind": kind, "title": title, "url": url, "ref": ref,
            "at": to_local(ts) if ts else None, "note": note,
        })

    def pr_info(repo, number):
        key = (repo, number)
        if key not in pr_cache:
            pr_cache[key] = api_get(
                f"repos/{OWNER}/{repo}/pulls/{number}", {},
            ) or {}
        detail = pr_cache[key]
        title = detail.get("title") or f"PR #{number}"
        url = detail.get("html_url") or (
            f"https://github.com/{OWNER}/{repo}/pull/{number}"
        )
        return title, url, detail

    # 先收集今天的合并 PR，用于把 release 归到触发发版的人。
    for event in events:
        if event.get("type") != "PullRequestEvent":
            continue
        ts = event_time(event)
        if not on_today(ts):
            continue
        payload = event.get("payload") or {}
        if payload.get("action") not in {"merged", "closed"}:
            continue
        number = payload.get("number") or \
            (payload.get("pull_request") or {}).get("number")
        if not number:
            continue
        repo = event_repo(event)
        title, url, detail = pr_info(repo, number)
        merged = payload.get("action") == "merged" or bool(detail.get("merged"))
        if not merged:
            continue
        actor = human(event_actor(event)) or human(login_of(detail.get("user")))
        merged_prs.setdefault(repo, []).append(
            (to_local(ts), actor, title, url),
        )

    # 提交：搜索默认分支 + 事件流里的分支 push，按 SHA 去重。
    for commit in search_commits():
        info = commit.get("commit") or {}
        ts = ((info.get("committer") or {}).get("date")
              or (info.get("author") or {}).get("date"))
        if not on_today(ts):
            continue
        if len(commit.get("parents") or []) > 1:
            continue
        message = (info.get("message") or "").strip().split("\n")[0]
        sha = commit.get("sha")
        if not message or not sha or merge_commit(message):
            continue
        login = human(login_of(commit.get("author"))) \
            or human(login_of(commit.get("committer")))
        if not login:
            continue
        seen_commits.add(sha)
        repo_obj = commit.get("repository") or {}
        repo = repo_obj.get("name") or \
            (repo_obj.get("fullName", "").split("/")[-1])
        url = commit.get("url") or (
            f"https://github.com/{OWNER}/{repo}/commit/{sha}"
        )
        add(login, "提交", message, url, f"{repo}@{sha[:7]}", ts)

    for event in events:
        ts = event_time(event)
        if not on_today(ts):
            continue
        etype = event.get("type") or ""
        payload = event.get("payload") or {}
        repo = event_repo(event)
        login = event_actor(event)

        if etype == "PushEvent":
            if not human(login):
                continue
            for commit in payload.get("commits") or []:
                sha = commit.get("sha")
                message = (commit.get("message") or "").strip().split("\n")[0]
                if not sha or not message or merge_commit(message):
                    continue
                if sha in seen_commits:
                    continue
                seen_commits.add(sha)
                url = commit.get("url") or (
                    f"https://github.com/{OWNER}/{repo}/commit/{sha}"
                )
                add(login, "提交", message, url, f"{repo}@{sha[:7]}", ts)

        elif etype == "IssuesEvent":
            action = payload.get("action")
            issue = payload.get("issue") or {}
            if action == "opened":
                add(login, "新建 issue", issue.get("title"),
                    issue.get("html_url"),
                    f"{repo}#{issue.get('number')}", ts)
            elif action == "closed":
                if not human(login):
                    login = first_human(item_assignees(issue))
                if not human(login):
                    login = login_of(issue.get("user"))
                add(login, "关闭 issue", issue.get("title"),
                    issue.get("html_url"),
                    f"{repo}#{issue.get('number')}", ts)

        elif etype == "PullRequestEvent":
            action = payload.get("action")
            pr = payload.get("pull_request") or {}
            number = payload.get("number") or pr.get("number")
            if not number:
                continue
            title, url, detail = pr_info(repo, number)
            if action == "opened":
                add(login, "新建 PR", title, url, f"{repo}#{number}", ts)
            elif action in {"merged", "closed"}:
                if not human(login):
                    login = login_of(detail.get("user")) \
                        or login_of(pr.get("user"))
                merged = action == "merged" or bool(detail.get("merged"))
                add(login, "合并 PR" if merged else "关闭 PR",
                    title, url, f"{repo}#{number}", ts)

        elif etype == "PullRequestReviewEvent":
            if payload.get("action") not in {"created", "submitted"}:
                continue
            pr = payload.get("pull_request") or {}
            number = pr.get("number")
            if not number:
                continue
            title, url, _ = pr_info(repo, number)
            review = payload.get("review") or {}
            state = (review.get("state") or "").upper()
            add(login, "Review", title, url, f"{repo}#{number}", ts,
                REVIEW_STATES.get(state, state.lower()))

        elif etype in {"IssueCommentEvent", "PullRequestReviewCommentEvent"}:
            if payload.get("action") != "created":
                continue
            comment = payload.get("comment") or {}
            item = payload.get("issue") or payload.get("pull_request") or {}
            number = item.get("number")
            title = item.get("title")
            url = item.get("html_url")
            if etype == "PullRequestReviewCommentEvent" and number:
                title, url, _ = pr_info(repo, number)
            excerpt = " ".join((comment.get("body") or "").split())[:50]
            add(login, "评论", title, comment.get("html_url") or url,
                f"{repo}#{number}", ts, excerpt)

        elif etype == "ReleaseEvent":
            if payload.get("action") != "published":
                continue
            release = payload.get("release") or {}
            when = to_local(ts)
            candidates = [
                entry for entry in merged_prs.get(repo, [])
                if entry[0] <= when
                and (when - entry[0]).total_seconds() <= 1800
                and human(entry[1])
            ]
            if candidates:
                login = max(candidates, key=lambda entry: entry[0])[1]
            else:
                login = login_of(release.get("author"))
            add(login, "发版", f"{repo} {release.get('tag_name')}",
                release.get("html_url"), "", ts)

    reports = []
    for login, entries in sorted(
        activities.items(), key=lambda kv: (-len(kv[1]), kv[0]),
    ):
        by_kind = {}
        for entry in entries:
            by_kind.setdefault(entry["kind"], []).append(entry)
        lines = [
            f"## 🌙 NexForm 晚报 · {TODAY}",
            f"> 👤 {real(login)} · 今日 {len(entries)} 项工作 · "
            f"[项目看板]({PROJECT_URL})",
            "",
        ]
        for kind in KIND_ORDER:
            group = by_kind.get(kind)
            if not group:
                continue
            group.sort(key=lambda entry: entry["at"] or NOW)
            lines.append(f"**{kind}（{len(group)}）**")
            for entry in group:
                when = entry["at"].strftime("%H:%M") if entry["at"] else ""
                note = f" · {md_escape(cut(entry['note'], 50))}" \
                    if entry["note"] else ""
                ref = f" · {entry['ref']}" if entry["ref"] else ""
                lines.append(
                    f"- [{md_escape(cut(entry['title']))}]({entry['url']})"
                    f"{ref}{note} · {when}"
                )
            lines.append("")
        reports.append((
            login,
            f"NexForm 晚报 · {real(login)}",
            "\n".join(lines).rstrip(),
        ))
    return reports


def fallback():
    if MODE == "morning":
        title = "NexForm 早安 · 今天没有待办"
        text = (
            f"## 🌅 NexForm 早安 · {TODAY}\n"
            f"> 今天没有指派到人的待办任务。\n\n[项目看板]({PROJECT_URL})"
        )
    else:
        title = "NexForm 晚报 · 今天没有工作记录"
        text = (
            f"## 🌙 NexForm 晚报 · {TODAY}\n"
            f"> 今天没有工作记录。\n\n[项目看板]({PROJECT_URL})"
        )
    return [(None, title, text)]


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


if not within_window():
    print(
        f"outside {MODE} window at {NOW:%H:%M} Asia/Shanghai; skipping",
        file=sys.stderr,
    )
    sys.exit(0)

reports = morning() if MODE == "morning" else evening()
if not reports:
    reports = fallback()

label = "早报" if MODE == "morning" else "晚报"
failed = []
for index, (login, title, text) in enumerate(reports):
    if index and not DRY_RUN and SEND_INTERVAL > 0:
        time.sleep(SEND_INTERVAL)
    payload = {
        "msgtype": "markdown",
        "markdown": {"title": title, "text": cap_markdown(text)},
    }
    target = real(login) if login else "兜底"
    if not send(payload, f"{label} · {target}"):
        failed.append(login or "fallback")

if failed:
    fail(f"failed to send {len(failed)} report(s): {','.join(failed)}")
