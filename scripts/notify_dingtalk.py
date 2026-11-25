#!/usr/bin/env python3
"""GitHub event -> DingTalk 自定义机器人，带真实姓名和 @ 人。

环境变量：
  DING_TOKEN             自定义机器人 access_token
  MEMBERS_JSON           成员与负责人配置，见 README
  GITHUB_EVENT_NAME      GitHub 自动提供
  GITHUB_EVENT_PATH      GitHub 自动提供
  DRY_RUN=1              只打印消息，不发送（本地测试用）
"""
import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

DING_TOKEN = os.environ.get("DING_TOKEN", "")
MEMBERS_JSON = os.environ.get("MEMBERS_JSON", "{}")
EVENT_NAME = os.environ.get("GITHUB_EVENT_NAME", "")
EVENT_PATH = os.environ.get("GITHUB_EVENT_PATH", "")
GITHUB_TOKEN = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPOSITORY = os.environ.get("GITHUB_REPOSITORY", "")
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
    raw_config = json.loads(MEMBERS_JSON or "{}")
except Exception:
    fail("MEMBERS_JSON is not valid JSON")

# 兼容老格式：{"login": {"name": ...}}
if "members" in raw_config or "owners" in raw_config:
    members = raw_config.get("members", {})
    owners_map = raw_config.get("owners", {})
else:
    members = raw_config
    owners_map = {}


def real(login):
    if not login:
        return "未知用户"
    name = members.get(login, {}).get("name")
    return f"{name}（{login}）" if name else login


def cut(text, limit=200):
    text = text or ""
    return text[:limit] + ("…" if len(text) > limit else "")


def mention(logins):
    """返回 atUserIds、atMobiles、正文里的 @ 文本、缺联系人。"""
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


def parse_codeowners(text):
    users, teams = [], []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        for token in line.split()[1:]:
            if token.startswith("@") and "/" in token:
                teams.append(token)
            elif token.startswith("@"):
                users.append(token[1:])
    return list(dict.fromkeys(users)), list(dict.fromkeys(teams))


def fetch_codeowners():
    """用 GITHUB_TOKEN 读当前仓库的 CODEOWNERS。"""
    if not (GITHUB_TOKEN and GITHUB_REPOSITORY):
        return [], []
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "nexform-notify",
    }
    for path in (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS"):
        url = (
            f"https://api.github.com/repos/{GITHUB_REPOSITORY}/contents/"
            + urllib.parse.quote(path)
        )
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            content = base64.b64decode(data.get("content", "")).decode("utf-8", "ignore")
            return parse_codeowners(content)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                continue
            print(f"CODEOWNERS fetch failed: HTTP {exc.code}", file=sys.stderr)
            return [], []
        except Exception as exc:
            print(f"CODEOWNERS fetch failed: {type(exc).__name__}", file=sys.stderr)
            return [], []
    return [], []


repo_obj = event.get("repository") or {}
repo_full = repo_obj.get("full_name", "")
repo_name = repo_obj.get("name", "")
default_branch = repo_obj.get("default_branch", "main")
owners = owners_map.get(repo_name) or owners_map.get(repo_full) or []

codeowners, teams = fetch_codeowners()
if teams:
    print(f"CODEOWNERS 团队暂不解析: {','.join(teams)}", file=sys.stderr)
owner_logins = codeowners or owners


def build():
    if EVENT_NAME == "issues":
        issue = event.get("issue") or {}
        action = event.get("action", "")
        actor = real((event.get("sender") or {}).get("login"))
        author = real((issue.get("user") or {}).get("login"))
        people = f"操作人：{actor}" if author == actor else f"作者：{author}\n操作人：{actor}"
        text = "\n".join([
            f"[NexForm] Issue {action}",
            f"#{issue.get('number')} {cut(issue.get('title'))}",
            f"仓库：{repo_full}",
            people,
            f"状态：{issue.get('state', '')}",
            f"链接：{issue.get('html_url', '')}",
        ])
        return text

    if EVENT_NAME == "issue_comment":
        issue = event.get("issue") or {}
        if issue.get("pull_request"):
            return None
        comment = event.get("comment") or {}
        author = real((comment.get("user") or {}).get("login"))
        return "\n".join([
            "[NexForm] Issue 新评论",
            f"#{issue.get('number')} {cut(issue.get('title'))}",
            f"仓库：{repo_full}",
            f"评论人：{author}",
            f"链接：{comment.get('html_url', '')}",
        ])

    if EVENT_NAME == "pull_request":
        action = event.get("action", "")
        if action not in {"opened", "closed", "reopened", "ready_for_review"}:
            return None
        pr = event.get("pull_request") or {}
        actor = real((event.get("sender") or {}).get("login"))
        author = real((pr.get("user") or {}).get("login"))
        people = f"操作人：{actor}" if author == actor else f"作者：{author}\n操作人：{actor}"
        return "\n".join([
            f"[NexForm] PR {action}",
            f"#{pr.get('number')} {cut(pr.get('title'))}",
            f"仓库：{repo_full}",
            people,
            f"链接：{pr.get('html_url', '')}",
        ])

    if EVENT_NAME == "push":
        ref = event.get("ref", "")
        if ref != f"refs/heads/{default_branch}":
            return None
        actor = real((event.get("sender") or {}).get("login"))
        commits = event.get("commits") or []
        return "\n".join([
            "[NexForm] Push",
            f"仓库：{repo_full}",
            f"分支：{ref.replace('refs/heads/', '')}",
            f"推送人：{actor}",
            f"提交数：{len(commits)}",
            f"链接：{event.get('compare', '')}",
        ])

    return None


body = build()
if not body:
    print("ignored")
    sys.exit(0)

actor_login = (event.get("sender") or {}).get("login", "")
ids, mobiles, tokens, missing = mention(list(owner_logins) + [actor_login])
if missing:
    print(f"缺少钉钉联系人: {','.join(missing)}", file=sys.stderr)
if tokens:
    body += "\n\n" + " ".join(tokens)

body = body[:5000]
payload = {
    "msgtype": "text",
    "text": {"content": body},
    "at": {
        "atUserIds": ids,
        "atMobiles": mobiles,
        "isAtAll": False,
    },
}

if DRY_RUN:
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    sys.exit(0)

token = urllib.parse.quote(DING_TOKEN, safe="")
url = f"https://oapi.dingtalk.com/robot/send?access_token={token}"
request = urllib.request.Request(
    url,
    data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
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
