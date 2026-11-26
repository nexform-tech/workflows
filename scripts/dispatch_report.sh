#!/usr/bin/env bash
# 从外部 cron 触发 NexForm 早报 / 晚报（workflow_dispatch）。
#
# 用途：GitHub 自己的 schedule 可能延迟数小时，用外部定时在
# 09:00 / 18:30 Asia/Shanghai 可靠触发。
#
# 依赖：gh CLI；GH_TOKEN 需要能对 nexform-tech/workflows 执行 workflow_dispatch
#       （fine-grained PAT 的 Actions: write，或 classic PAT 的 repo + workflow）。
#
# 用法：
#   GH_TOKEN=... ./dispatch_report.sh morning
#   GH_TOKEN=... ./dispatch_report.sh evening
set -euo pipefail

REPO="${REPO:-nexform-tech/workflows}"
mode="${1:-}"

case "$mode" in
  morning) workflow="morning-report.yml" ;;
  evening) workflow="evening-report.yml" ;;
  *)
    echo "usage: $0 morning|evening" >&2
    exit 2
    ;;
esac

: "${GH_TOKEN:?GH_TOKEN is required (Actions: write on $REPO)}"

exec gh workflow run "$workflow" --repo "$REPO" --ref main
